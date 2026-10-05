"""Re-run failed tests from a consolidated report and emit a final consolidated report.

Usage
-----
  # Auto-find the most recent consolidated results JSON:
  python runners/rerun_failed_tests.py

  # Point at a specific consolidated JSON:
  python runners/rerun_failed_tests.py results/agent_results_consolidated_*.json

Run from the project root (agents_test_automation/).

Workflow
--------
1. Load the most recent consolidated report (96 passed, 97 failed).
2. Keep the 96 passing tests as-is — do NOT re-run them.
3. Re-run all 97 failed tests with intelligent per-test retry on 429.
4. Merge: 96 carried-forward results + 97 fresh retry results = 193 total.
5. Write final consolidated JSON + HTML reports.
6. Print before/after summary.
"""

import glob
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ── Path: add project root so package imports resolve correctly ────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import Settings
from harness.agent_test_harness import AgentTestHarness
from harness.graders.judge_grader import JudgeGrader
from harness.graders.keyword_grader import KeywordGrader
from runners.test_runner import TestRunner
from tests.test_definitions import load_test_suite
from utils.http_client import GatewayHTTPClient
from utils.logger import setup_logger

# ── Constants ─────────────────────────────────────────────────────────────────
_CSV_PATH = "tests/master_suite.csv"
_RETRY_DELAY_SECONDS = 10       # legacy fixed-delay mode
_MAX_RETRY_ATTEMPTS = 5         # intelligent retry: max attempts per test
_INITIAL_RETRY_WAIT = 30        # seconds to wait before 2nd attempt on 429
_RETRY_BACKOFF_SECONDS = 30     # extra seconds added per subsequent retry
_SEP = "=" * 52


# ─────────────────────────────────────────────────────────────────────────────
# Report discovery / loading
# ─────────────────────────────────────────────────────────────────────────────

def find_most_recent_report(results_dir: str) -> str:
    """Return the path to the latest non-consolidated results JSON in results_dir.

    Raises FileNotFoundError if no matching file exists.
    """
    pattern = os.path.join(results_dir, "agent_results_*.json")
    candidates = [
        f for f in glob.glob(pattern)
        if "consolidated" not in os.path.basename(f)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No results JSON found in '{results_dir}'. "
            "Run the full test suite first: python run_tests.py"
        )
    return max(candidates, key=os.path.getmtime)


def find_most_recent_consolidated_report(results_dir: str) -> str:
    """Return the path to the latest consolidated (non-final) results JSON.

    Matches ``agent_results_consolidated_*.json`` but excludes
    ``final_consolidated`` files.

    Raises FileNotFoundError if no matching file exists.
    """
    pattern = os.path.join(results_dir, "agent_results_consolidated_*.json")
    candidates = [
        f for f in glob.glob(pattern)
        if "final_consolidated" not in os.path.basename(f)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No consolidated results JSON found in '{results_dir}'. "
            "Run rerun_failed_tests.py first to generate a consolidated report."
        )
    return max(candidates, key=os.path.getmtime)


def load_report_json(path: str) -> Dict:
    """Load and return the parsed JSON report at path."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# 429 detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_429_reason(reason: str) -> bool:
    r = reason.lower()
    return (
        "429" in r
        or "agent is busy" in r
        or "rate limit" in r
        or "too many requests" in r
    )


def _is_429_result(result: Dict) -> bool:
    return _is_429_reason(result.get("grading_reason") or "")


def extract_429_tests(report: Dict) -> List[str]:
    """Return the test_ids of tests that failed due to a 429 / busy-agent error."""
    ids: List[str] = []
    for ft in report.get("failed_tests", []):
        if _is_429_reason(ft.get("reason", "")):
            ids.append(ft["test_id"])
    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Test suite filtering
# ─────────────────────────────────────────────────────────────────────────────

def filter_test_suite(test_suite: List[Dict], test_ids: Set[str]) -> List[Dict]:
    """Return only the test cases whose test_id is in test_ids, preserving CSV order."""
    return [tc for tc in test_suite if tc["test_id"] in test_ids]


# ─────────────────────────────────────────────────────────────────────────────
# Result reconstruction / conversion
# ─────────────────────────────────────────────────────────────────────────────

def reconstruct_original_results(report: Dict, test_suite: List[Dict]) -> List[Dict]:
    """Build a per-test results list from the aggregated JSON report.

    The JSON only stores failed test details; passed tests are reconstructed
    as minimal synthetic dicts (no duration/tokens — not persisted in JSON).
    Result order matches the test_suite (CSV) order.
    """
    failed_map: Dict[str, Dict] = {
        ft["test_id"]: ft for ft in report.get("failed_tests", [])
    }
    results: List[Dict] = []
    for tc in test_suite:
        tid = tc["test_id"]
        if tid in failed_map:
            ft = failed_map[tid]
            results.append({
                "test_id": tid,
                "test_name": ft["test_name"],
                "dimension": ft["dimension"],
                "priority": ft["priority"],
                "passed": False,
                "duration_ms": 0.0,
                "tokens_used": {},
                "error": None,
                "grading_reason": ft["reason"],
                "grading_details": ft.get("details", {}),
                "response_sample": "",
                "agent_response": "",
            })
        else:
            results.append({
                "test_id": tid,
                "test_name": tc["test_name"],
                "dimension": tc["dimension"],
                "priority": tc["priority"],
                "passed": True,
                "duration_ms": 0.0,
                "tokens_used": {},
                "error": None,
                "grading_reason": "Passed in original run",
                "grading_details": {},
                "response_sample": "",
                "agent_response": "",
            })
    return results


def convert_passed_tests_to_results(passed_tests: List[Dict]) -> List[Dict]:
    """Convert passed_tests entries from a JSON report into harness-compatible result dicts."""
    return [
        {
            "test_id": t["test_id"],
            "test_name": t["test_name"],
            "dimension": t["dimension"],
            "priority": t["priority"],
            "passed": True,
            "duration_ms": t.get("duration_ms", 0.0),
            "tokens_used": t.get("tokens_used") or {},
            "error": None,
            "grading_reason": "Passed in original run",
            "grading_details": {},
            "response_sample": "",
            "agent_response": "",
        }
        for t in passed_tests
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Retry execution
# ─────────────────────────────────────────────────────────────────────────────

def run_retry_suite(
    harness: AgentTestHarness,
    retry_suite: List[Dict],
    graders: Dict,
    logger: logging.Logger,
    delay_seconds: int = _RETRY_DELAY_SECONDS,
) -> List[Dict]:
    """Execute each test in retry_suite with a fixed delay between tests.

    Legacy mode — uses a flat inter-test delay, no per-test 429 retry.
    """
    total = len(retry_suite)
    results: List[Dict] = []
    logger.info("Retry suite: %d tests, %ds inter-test delay", total, delay_seconds)

    for idx, test_case in enumerate(retry_suite, start=1):
        test_id = test_case.get("test_id", f"UNKNOWN-{idx}")
        test_name = test_case.get("test_name", "")

        try:
            result = harness.run_test_case(test_case, graders)
        except Exception as exc:
            logger.error("Unhandled exception in %s: %s", test_id, exc, exc_info=True)
            result = AgentTestHarness._make_error_result(test_case, str(exc))

        results.append(result)

        status = "PASS" if result["passed"] else "FAIL"
        duration_s = result["duration_ms"] / 1000
        suffix = f" ({result.get('grading_reason', '')})" if not result["passed"] else ""
        logger.info(
            "[%d/%d] %s %s ... %s (%.1fs)%s",
            idx, total, test_id, test_name, status, duration_s, suffix,
        )
        print(f"  [{idx}/{total}] {test_id} ... {status}{suffix}")

        if idx < total:
            time.sleep(delay_seconds)

    return results


def run_retry_suite_with_intelligent_retry(
    harness: AgentTestHarness,
    retry_suite: List[Dict],
    graders: Dict,
    logger: logging.Logger,
    max_attempts: int = _MAX_RETRY_ATTEMPTS,
    initial_wait: int = _INITIAL_RETRY_WAIT,
    backoff: int = _RETRY_BACKOFF_SECONDS,
) -> List[Dict]:
    """Execute each test, retrying immediately on 429 with increasing backoff.

    For each test:
    - Attempt 1: run normally.
    - If result is 429, wait ``initial_wait`` seconds and retry.
    - Each subsequent 429 adds another ``backoff`` seconds (30, 60, 90 …).
    - Stop retrying when we get a non-429 result or exhaust max_attempts.

    A small 2-second inter-test delay is used between different tests.
    """
    total = len(retry_suite)
    results: List[Dict] = []
    logger.info(
        "Intelligent retry suite: %d tests, up to %d attempts per test",
        total, max_attempts,
    )

    for idx, test_case in enumerate(retry_suite, start=1):
        test_id = test_case.get("test_id", f"UNKNOWN-{idx}")
        test_name = test_case.get("test_name", "")
        result = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = harness.run_test_case(test_case, graders)
            except Exception as exc:
                logger.error(
                    "Exception in %s (attempt %d/%d): %s",
                    test_id, attempt, max_attempts, exc, exc_info=True,
                )
                result = AgentTestHarness._make_error_result(test_case, str(exc))

            if not _is_429_result(result):
                break  # success or non-429 failure — move on

            if attempt < max_attempts:
                wait = initial_wait + (attempt - 1) * backoff
                logger.warning(
                    "%s returned 429 (attempt %d/%d) — waiting %ds before retry",
                    test_id, attempt, max_attempts, wait,
                )
                print(
                    f"  [{idx}/{total}] {test_id} 429 "
                    f"(attempt {attempt}/{max_attempts}), waiting {wait}s..."
                )
                time.sleep(wait)

        results.append(result)

        status = "PASS" if result["passed"] else "FAIL"
        duration_s = result["duration_ms"] / 1000
        suffix = f" ({result.get('grading_reason', '')})" if not result["passed"] else ""
        logger.info(
            "[%d/%d] %s %s ... %s (%.1fs)%s",
            idx, total, test_id, test_name, status, duration_s, suffix,
        )
        print(f"  [{idx}/{total}] {test_id} ... {status}{suffix}")

        if idx < total:
            time.sleep(2)  # small inter-test cooldown between successful tests

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Merge (legacy — used by the old single-pass 429-only workflow)
# ─────────────────────────────────────────────────────────────────────────────

def merge_results(
    original_results: List[Dict],
    retry_results: List[Dict],
) -> Tuple[List[Dict], int, int]:
    """Replace 429-failed entries in original_results with fresh retry_results.

    Returns:
        (merged_list, improved_count, still_failing_count)
    """
    idx_map: Dict[str, int] = {r["test_id"]: i for i, r in enumerate(original_results)}
    merged = list(original_results)
    improved = 0
    still_failing = 0

    for retry in retry_results:
        pos = idx_map.get(retry["test_id"])
        if pos is None:
            continue
        merged[pos] = retry
        if not _is_429_result(retry):
            improved += 1
        else:
            still_failing += 1

    return merged, improved, still_failing


# ─────────────────────────────────────────────────────────────────────────────
# Consolidated report generation
# ─────────────────────────────────────────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H_%M_%SZ")


def generate_consolidated_json(
    merged_results: List[Dict],
    runner: "TestRunner",
    original_report: Dict,
    tests_rerun: int,
    tests_improved: int,
    output_dir: str,
    ts: str,
    label: str = "consolidated",
) -> str:
    """Aggregate merged_results into a JSON report, add consolidated metadata, and write it.

    Uses runner.generate_json_report() to produce a fully enriched report
    (including passed_tests and failed_tests with full detail fields).

    Returns the absolute path of the written file.
    """
    report = json.loads(runner.generate_json_report(merged_results))
    # Consolidated-specific metadata
    report["consolidated"] = True
    report["original_run_date"] = original_report.get("execution_timestamp", "")
    report["rerun_date"] = datetime.now(timezone.utc).isoformat()
    report["tests_rerun"] = tests_rerun
    report["tests_improved"] = tests_improved

    filename = f"agent_results_{label}_{ts}.json"
    path = os.path.join(output_dir, filename)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return os.path.abspath(path)


def generate_consolidated_html(
    merged_results: List[Dict],
    runner: "TestRunner",
    output_dir: str,
    ts: str,
    label: str = "consolidated",
) -> str:
    """Render the v2 HTML dashboard for merged_results and write it.

    Returns the absolute path of the written file.
    """
    html_content = runner.generate_html_report(merged_results)
    filename = f"agent_results_{label}_{ts}.html"
    path = os.path.join(output_dir, filename)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html_content, encoding="utf-8")
    return os.path.abspath(path)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    """Entry point. Returns 0 on success, 1 on error."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    boot_log = logging.getLogger("rerun")

    # ── 1. Load configuration ──────────────────────────────────────────────
    try:
        config = Settings()
    except Exception as exc:
        boot_log.critical("Failed to load configuration: %s", exc)
        return 1

    logger = setup_logger("rerun", config.logs_dir, config.log_level)
    results_dir = config.results_dir

    # ── 2. Find and load consolidated report ──────────────────────────────
    if len(sys.argv) > 1:
        report_path = sys.argv[1]
        if not os.path.isfile(report_path):
            logger.critical("Specified report not found: %s", report_path)
            return 1
    else:
        try:
            report_path = find_most_recent_consolidated_report(results_dir)
        except FileNotFoundError as exc:
            logger.critical("%s", exc)
            return 1

    logger.info("Loading consolidated report: %s", report_path)
    try:
        original_report = load_report_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.critical("Cannot parse report JSON at %s: %s", report_path, exc)
        return 1

    # ── 3. Extract passed and failed test data ─────────────────────────────
    passed_tests_data: List[Dict] = original_report.get("passed_tests", [])
    failed_tests_data: List[Dict] = original_report.get("failed_tests", [])
    failed_test_ids: Set[str] = {t["test_id"] for t in failed_tests_data}

    # Load full test suite (needed for filter + report enrichment)
    try:
        full_test_suite = load_test_suite(_CSV_PATH)
    except (FileNotFoundError, ValueError) as exc:
        logger.critical("Failed to load test suite from %s: %s", _CSV_PATH, exc)
        return 1

    # Older consolidated reports (before passed_tests was added) won't have
    # that field — reconstruct from the full suite minus known-failed IDs.
    if not passed_tests_data:
        logger.info("No passed_tests in report — reconstructing from test suite")
        passed_tests_data = [
            {
                "test_id": tc["test_id"],
                "test_name": tc["test_name"],
                "dimension": tc["dimension"],
                "priority": tc["priority"],
                "passed": True,
                "duration_ms": 0.0,
                "tokens_used": {},
            }
            for tc in full_test_suite
            if tc["test_id"] not in failed_test_ids
        ]

    print(_SEP)
    print("Original Results (from consolidated report):")
    print(f"  Passed: {len(passed_tests_data)} (NOT re-run, carried forward)")
    print(f"  Failed: {len(failed_tests_data)} (RE-RUN with retry logic)")
    print(f"  Total:  {len(passed_tests_data) + len(failed_tests_data)}")
    print(_SEP)

    if not failed_tests_data:
        print("No failed tests to retry. Nothing to do.")
        return 0

    # ── 4. Filter test suite to failed tests only ─────────────────────────
    retry_suite = filter_test_suite(full_test_suite, failed_test_ids)

    # ── 5. Initialise graders and HTTP client ─────────────────────────────
    keyword_grader = KeywordGrader()

    judge_grader: Optional[JudgeGrader] = None
    if config.claude_api_key:
        judge_grader = JudgeGrader(
            claude_api_key=config.claude_api_key,
            model=config.judge_model,
            logger=logger,
            cache_dir=config.logs_dir,
        )
        logger.info("JudgeGrader enabled (model: %s)", config.judge_model)
    else:
        logger.warning("CLAUDE_API_KEY not set — llm_rubric tests will be skipped")

    graders = {"keyword": keyword_grader, "judge": judge_grader, "semantic": None}

    try:
        http_client = GatewayHTTPClient(config, logger)
    except Exception as exc:
        logger.critical("HTTP client initialisation failed: %s", exc)
        return 1

    harness = AgentTestHarness(config, http_client, logger)

    # ── 6. Re-run ALL failed tests with intelligent retry ─────────────────
    print(f"\nRe-running {len(retry_suite)} failed tests with intelligent retry logic...")
    print(_SEP)

    try:
        retry_results = run_retry_suite_with_intelligent_retry(
            harness, retry_suite, graders, logger,
        )
    except Exception as exc:
        logger.critical("Retry run crashed unexpectedly: %s", exc, exc_info=True)
        http_client.close()
        return 1

    # ── 7. Analyse re-run outcomes ────────────────────────────────────────
    now_passing = sum(1 for r in retry_results if r["passed"])
    still_failing_results = [r for r in retry_results if not r["passed"]]
    still_failing = len(still_failing_results)
    still_429 = sum(1 for r in still_failing_results if _is_429_result(r))

    # ── 8. Build merged results: carried-forward passed + all retry ───────
    passed_results = convert_passed_tests_to_results(passed_tests_data)
    merged = passed_results + retry_results

    # ── 9. Generate final consolidated reports ────────────────────────────
    runner_report = TestRunner(config, http_client, full_test_suite, graders, logger)
    ts = _timestamp()

    try:
        json_path = generate_consolidated_json(
            merged,
            runner_report,
            original_report,
            tests_rerun=len(retry_suite),
            tests_improved=now_passing,
            output_dir=results_dir,
            ts=ts,
            label="final_consolidated",
        )
        html_path = generate_consolidated_html(
            merged, runner_report, results_dir, ts,
            label="final_consolidated",
        )
    except Exception as exc:
        logger.error("Failed to write final consolidated reports: %s", exc, exc_info=True)
        http_client.close()
        return 1

    # ── 10. Final summary ─────────────────────────────────────────────────
    original_pass_count = len(passed_tests_data)
    final_pass_count = original_pass_count + now_passing
    total_tests = len(merged)
    pass_rate = (final_pass_count / total_tests * 100) if total_tests else 0.0

    print(_SEP)
    print(f"Original Results ({original_pass_count} passed, {len(failed_tests_data)} failed):")
    print(f"  ├─ Passed: {original_pass_count} (NOT re-run, carried forward)")
    print(f"  └─ Failed: {len(failed_tests_data)} (RE-RUN with retry logic)")
    print(f"Re-run Results ({len(retry_suite)} failed tests retried):")
    print(f"  ├─ Now Passing: {now_passing} tests improved")
    print(f"  ├─ Still Failing: {still_failing} tests failed again")
    print(f"  └─ Rate Limited: {still_429} tests hit 429 ceiling")
    print(f"Final Consolidated Results (all {total_tests}):")
    print(f"  ├─ Total Tests: {total_tests}")
    print(f"  ├─ Passed: {original_pass_count} + {now_passing} = {final_pass_count}")
    print(f"  ├─ Failed: {still_failing}")
    print(f"  └─ Pass Rate: {pass_rate:.1f}%")
    print("Report Paths:")
    print(f"  ├─ JSON: {json_path}")
    print(f"  └─ HTML: {html_path}")
    print(_SEP)

    http_client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)
    except Exception as exc:
        print(f"\nUnexpected error: {exc}")
        traceback.print_exc()
        sys.exit(1)
