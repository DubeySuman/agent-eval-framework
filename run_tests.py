"""Entry point — initialise all components and execute the Nova test suite."""

import argparse
import sys
import time
import traceback

from config.settings import Settings
from harness.graders.judge_grader import JudgeGrader
from harness.graders.keyword_grader import KeywordGrader
from runners.test_runner import TestRunner
from tests.test_definitions import load_test_suite
from utils.http_client import GatewayHTTPClient
from utils.logger import setup_logger

_CSV_PATH = "tests/master_suite.csv"
_REGRESSION_CSV_PATH = "tests/regression_suite.csv"
_PASS_THRESHOLD = 95.0  # exit 0 if pass rate >= this percentage

_SEP = "=" * 48

_SUITE_PATHS = {
    "master": _CSV_PATH,
    "regression": _REGRESSION_CSV_PATH,
}


def load_test_suite_by_name(suite_name: str):
    """Return the loaded test suite for the given suite name.

    Args:
        suite_name: One of ``'master'`` (all 193 tests) or
            ``'regression'`` (~57 critical-path tests).

    Raises:
        ValueError: If suite_name is not recognised.
        FileNotFoundError: If the CSV file does not exist.
    """
    if suite_name not in _SUITE_PATHS:
        raise ValueError(
            f"Unknown suite '{suite_name}'. Valid options: {list(_SUITE_PATHS)}"
        )
    return load_test_suite(_SUITE_PATHS[suite_name])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Nova test automation suite."
    )
    parser.add_argument(
        "--suite",
        choices=["master", "regression"],
        default="master",
        help=(
            "Test suite to run. 'master' runs all 193 tests; "
            "'regression' runs the ~57-test critical-path suite. Default: master."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which tests would run without executing them, then exit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N tests from the selected suite.",
    )
    parser.add_argument(
        "--agent",
        default="nova",
        help="Agent to test (key from config/agents.yaml). Default: nova.",
    )
    return parser.parse_args()


def main() -> int:
    """Load config, run the selected test suite, write reports, print summary.

    Returns:
        Exit code:
            0 — pass rate >= 95 % (or 100 %), or --dry-run
            1 — pass rate < 95 % or a fatal initialisation error
    """
    args = _parse_args()

    # ------------------------------------------------------------------
    # 1. Bootstrap logging with a temporary basic config so early errors
    #    are visible even before Settings is loaded.
    # ------------------------------------------------------------------
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    bootstrap_log = logging.getLogger("bootstrap")

    # ------------------------------------------------------------------
    # 2. Load configuration
    # ------------------------------------------------------------------
    try:
        config = Settings()
    except (ValueError, Exception) as exc:
        bootstrap_log.critical("Failed to load configuration: %s", exc)
        return 1

    # Resolve agent config and override model/endpoint for this run.
    try:
        agent_cfg = config.get_agent_config(args.agent)
    except ValueError as exc:
        bootstrap_log.critical("Invalid --agent value: %s", exc)
        return 1
    config._model = agent_cfg["model"]
    # agents.yaml stores the full chat-completions URL; the HTTP client appends
    # "/chat/completions" itself, so strip it here to get the base URL.
    config._api_base_url = agent_cfg["endpoint"].removesuffix("/chat/completions")
    agent_display_name = agent_cfg["name"]

    # Replace bootstrap logger with the real file+console logger.
    logger = setup_logger("test_harness", config.logs_dir, config.log_level)

    # ------------------------------------------------------------------
    # 3. Load test suite
    # ------------------------------------------------------------------
    csv_path = _SUITE_PATHS[args.suite]
    try:
        test_suite = load_test_suite_by_name(args.suite)
        logger.info(
            "Loaded %d tests from %s (suite=%s)",
            len(test_suite), csv_path, args.suite,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.critical("Failed to load test suite: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # 3a. Apply --limit if specified
    # ------------------------------------------------------------------
    if args.limit:
        test_suite = test_suite[:args.limit]
        logger.info("--limit %d applied: running first %d tests", args.limit, len(test_suite))

    # ------------------------------------------------------------------
    # 3b. Dry-run: print what would run and exit
    # ------------------------------------------------------------------
    if args.dry_run:
        print(_SEP)
        print(f"DRY RUN — suite: {args.suite}  agent: {args.agent}")
        print(_SEP)
        print(f"  Agent:      {agent_display_name} ({args.agent})")
        print(f"  CSV:        {csv_path}")
        print(f"  Test count: {len(test_suite)}")
        print()
        for tc in test_suite:
            print(f"  [{tc['priority']:<8}] {tc['test_id']:<12} {tc['test_name']}")
        print()
        print(f"Total: {len(test_suite)} tests (not executed)")
        return 0

    # ------------------------------------------------------------------
    # 4. Initialise graders
    # ------------------------------------------------------------------
    keyword_grader = KeywordGrader()

    judge_grader: JudgeGrader | None = None
    if config.claude_api_key:
        judge_grader = JudgeGrader(
            claude_api_key=config.claude_api_key,
            model=config.judge_model,
            logger=logger,
            cache_dir=config.logs_dir,
        )
        logger.info("JudgeGrader enabled (model: %s)", config.judge_model)
    else:
        logger.warning(
            "CLAUDE_API_KEY not set — llm_rubric and hybrid tests will be skipped"
        )

    graders = {
        "keyword": keyword_grader,
        "judge": judge_grader,
        "semantic": None,  # SemanticGrader not yet implemented
    }
    logger.info("Graders initialised")

    # ------------------------------------------------------------------
    # 5. Initialise HTTP client
    # ------------------------------------------------------------------
    try:
        http_client = GatewayHTTPClient(config, logger)
        logger.info("HTTP client initialised")
    except Exception as exc:
        logger.critical("Failed to initialise HTTP client: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # 6. Initialise runner
    # ------------------------------------------------------------------
    runner = TestRunner(config, http_client, test_suite, graders, logger)
    logger.info("Test runner initialised")

    # ------------------------------------------------------------------
    # 7. Print run header
    # ------------------------------------------------------------------
    judge_status = "Enabled" if judge_grader else "Disabled (no CLAUDE_API_KEY)"
    print(_SEP)
    print("ACME AGENT TEST AUTOMATION")
    print(_SEP)
    print(f"  Agent:        {agent_display_name} ({args.agent})")
    print(f"  Suite:        {args.suite} ({len(test_suite)} tests)")
    print(f"  Endpoint:     {config.api_base_url}")
    print(f"  Model:        {config.model}")
    print(f"  Timeout:      {config.timeout}s")
    print(f"  Max Retries:  {config.max_retries}")
    print(f"  Judge Grader: {judge_status}")
    print(_SEP)
    print("  Running tests...")
    print(_SEP)

    # ------------------------------------------------------------------
    # 8. Execute suite
    # ------------------------------------------------------------------
    start_time = time.time()
    try:
        results = runner.run_all_tests()
    except Exception as exc:
        logger.critical("Test runner crashed unexpectedly: %s", exc, exc_info=True)
        http_client.close()
        return 1
    elapsed = time.time() - start_time
    logger.info("All %d tests completed in %.1fs", len(results), elapsed)

    # ------------------------------------------------------------------
    # 9. Generate and save reports
    # ------------------------------------------------------------------
    try:
        report_paths = runner.save_reports(results, suite_name=args.suite)
        logger.info("JSON report: %s", report_paths["json_path"])
        logger.info("HTML report: %s", report_paths["html_path"])
    except Exception as exc:
        logger.error("Failed to write reports: %s", exc)
        # Non-fatal — continue to summary and exit code.

    # ------------------------------------------------------------------
    # 10. Print summary
    # ------------------------------------------------------------------
    runner.print_summary(results)

    # ------------------------------------------------------------------
    # 11. Compute exit code
    # ------------------------------------------------------------------
    http_client.close()

    if not results:
        logger.error("No results collected — treating as failure")
        return 1

    passed = sum(1 for r in results if r["passed"])
    pass_rate = passed / len(results) * 100
    return 0 if pass_rate >= _PASS_THRESHOLD else 1


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nTest run interrupted by user")
        sys.exit(130)
    except Exception as exc:
        print(f"\nUnexpected error: {exc}")
        traceback.print_exc()
        sys.exit(1)
