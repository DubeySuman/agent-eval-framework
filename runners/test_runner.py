"""Test orchestrator — sequential execution, JSON + HTML report generation."""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader

from config.settings import Settings
from harness.agent_test_harness import AgentTestHarness
from utils.http_client import GatewayHTTPClient

# Priority display order for reports.
_PRIORITY_ORDER = ("Critical", "High", "Medium", "Low")

# Jinja2 template is in the same directory as this file.
_TEMPLATE_DIR = Path(__file__).parent
_TEMPLATE_FILE = "report_template_v2.html"


class TestRunner:
    """Orchestrate sequential execution of the full test suite and emit reports.

    Concurrency is intentionally 1 (sequential) to respect rate limits and
    produce a deterministic execution timeline in reports.

    Usage::

        runner = TestRunner(config, http_client, test_suite, graders, logger)
        results = runner.run_all_tests()
        paths = runner.save_reports(results)
        runner.print_summary(results)
    """

    def __init__(
        self,
        config: Settings,
        http_client: GatewayHTTPClient,
        test_suite: List[Dict],
        graders: Dict,
        logger: Logger,
    ) -> None:
        """Wire up the runner with all dependencies.

        Args:
            config: Validated Settings instance.
            http_client: Configured GatewayHTTPClient ready to make API calls.
            test_suite: List of test case dicts from :func:`tests.test_definitions.load_test_suite`.
            graders: Dict of grader instances (``"keyword"``, ``"judge"``, ``"semantic"``).
            logger: Caller-supplied logger for structured output.
        """
        self._config = config
        self._test_suite = test_suite
        self._graders = graders
        self._log = logger
        self._harness = AgentTestHarness(config, http_client, logger)

        # Set by run_all_tests(); used when embedding duration in reports.
        self._start_time: Optional[datetime] = None
        self._end_time: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_all_tests(self) -> List[Dict]:
        """Execute every test case sequentially and collect results.

        Catches per-test exceptions so one bad test never aborts the suite.
        Progress is logged after each test in the format::

            [N/193] TEST-ID Name ... PASS (2.3s)

        Returns:
            List of result dicts, one per test case.
        """
        total = len(self._test_suite)
        results: List[Dict] = []
        self._start_time = datetime.now(timezone.utc)

        self._log.info("Starting test suite: %d tests", total)

        for idx, test_case in enumerate(self._test_suite, start=1):
            test_id = test_case.get("test_id", f"UNKNOWN-{idx}")
            test_name = test_case.get("test_name", "")

            try:
                result = self._harness.run_test_case(test_case, self._graders)
                time.sleep(2)  # Avoid HTTP 429 — respect API concurrency limit of 1
            except Exception as exc:
                self._log.error(
                    "Unhandled exception in %s — recording as failure: %s",
                    test_id,
                    exc,
                    exc_info=True,
                )
                result = self._make_error_result(test_case, str(exc))

            results.append(result)

            status = "PASS" if result["passed"] else "FAIL"
            duration_s = result["duration_ms"] / 1000
            reason = f" ({result.get('grading_reason', '')})" if not result["passed"] else ""
            self._log.info(
                "[%d/%d] %s %s ... %s (%.1fs)%s",
                idx,
                total,
                test_id,
                test_name,
                status,
                duration_s,
                reason,
            )

        self._end_time = datetime.now(timezone.utc)
        elapsed = (self._end_time - self._start_time).total_seconds()
        passed = sum(1 for r in results if r["passed"])
        self._log.info(
            "Suite complete: %d/%d passed (%.1f%%) in %.1fs",
            passed,
            total,
            (passed / total * 100) if total else 0,
            elapsed,
        )
        return results

    # ------------------------------------------------------------------
    # JSON report
    # ------------------------------------------------------------------

    def generate_json_report(self, results: List[Dict]) -> str:
        """Aggregate results and serialise to a pretty-printed JSON string.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.

        Returns:
            Pretty-printed JSON string.
        """
        report = self._harness.generate_report(results)
        report["test_suite_file"] = "tests/master_suite.csv"
        report["execution_timestamp"] = (
            self._start_time.isoformat() if self._start_time else datetime.now(timezone.utc).isoformat()
        )
        if self._start_time and self._end_time:
            report["execution_duration_seconds"] = round(
                (self._end_time - self._start_time).total_seconds(), 2
            )
        report["environment"] = {
            "api_base_url": self._config.api_base_url,
            "model": self._config.model,
            "timeout_seconds": self._config.timeout,
        }

        test_suite_map = {tc["test_id"]: tc for tc in self._test_suite}

        failed_tests_detailed = []
        passed_tests_summary = []

        for result in results:
            if result["passed"]:
                passed_tests_summary.append({
                    "test_id": result["test_id"],
                    "test_name": result["test_name"],
                    "dimension": result["dimension"],
                    "priority": result["priority"],
                    "passed": True,
                    "duration_ms": result.get("duration_ms", 0),
                    "tokens_used": result.get("tokens_used") or {},
                })
            else:
                tc = test_suite_map.get(result["test_id"], {})
                failed_tests_detailed.append({
                    "test_id": result["test_id"],
                    "test_name": result["test_name"],
                    "dimension": result["dimension"],
                    "priority": result["priority"],
                    "reason": result.get("grading_reason") or result.get("error") or "Unknown",
                    "details": result.get("grading_details") or {},
                    "agent_response": result.get("agent_response") or "",
                    "prompt": tc.get("prompt", ""),
                    "expected_behavior": tc.get("expected_behavior", ""),
                    "assertion_method": tc.get("assertion_method", ""),
                    "should_contain": tc.get("should_contain") or [],
                    "should_not_contain": tc.get("should_not_contain") or [],
                    "grader_rubric": tc.get("grader_rubric", ""),
                    "duration_ms": result.get("duration_ms", 0),
                    "tokens_used": result.get("tokens_used") or {},
                })

        report["failed_tests"] = failed_tests_detailed
        report["passed_tests"] = passed_tests_summary

        return json.dumps(report, indent=2, default=str)

    def write_json_report(
        self,
        results: List[Dict],
        output_dir: Optional[str] = None,
        suite_name: str = "master",
    ) -> str:
        """Write the JSON report to disk and return the output path.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.
            output_dir: Directory to write to; defaults to ``config.results_dir``.
            suite_name: Suite identifier embedded in the filename (e.g. ``'master'``,
                ``'regression'``).

        Returns:
            Absolute path to the written file.
        """
        out_dir = output_dir or self._config.results_dir
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        ts = self._safe_timestamp()
        filename = f"agent_{suite_name}_{ts}.json"
        output_path = os.path.join(out_dir, filename)

        content = self.generate_json_report(results)
        Path(output_path).write_text(content, encoding="utf-8")
        self._log.info("JSON report written to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # HTML report
    # ------------------------------------------------------------------

    def generate_html_report(self, results: List[Dict]) -> str:
        """Render results as an HTML dashboard string using Jinja2.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.

        Returns:
            Full HTML document as a string.
        """
        report = self._harness.generate_report(results)
        context = self._build_template_context(results, report)

        env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=True,
        )
        template = env.get_template(_TEMPLATE_FILE)
        return template.render(**context)

    def write_html_report(
        self,
        results: List[Dict],
        output_dir: Optional[str] = None,
        suite_name: str = "master",
    ) -> str:
        """Write the HTML report to disk and return the output path.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.
            output_dir: Directory to write to; defaults to ``config.results_dir``.
            suite_name: Suite identifier embedded in the filename (e.g. ``'master'``,
                ``'regression'``).

        Returns:
            Absolute path to the written file.
        """
        out_dir = output_dir or self._config.results_dir
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        ts = self._safe_timestamp()
        filename = f"agent_{suite_name}_{ts}.html"
        output_path = os.path.join(out_dir, filename)

        content = self.generate_html_report(results)
        Path(output_path).write_text(content, encoding="utf-8")
        self._log.info("HTML report written to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def save_reports(
        self,
        results: List[Dict],
        output_dir: Optional[str] = None,
        suite_name: str = "master",
    ) -> Dict[str, str]:
        """Write both JSON and HTML reports and return their paths.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.
            output_dir: Directory to write to; defaults to ``config.results_dir``.
            suite_name: Suite identifier embedded in filenames (e.g. ``'master'``,
                ``'regression'``). Both files share the same base name.

        Returns:
            ``{"json_path": str, "html_path": str}``
        """
        json_path = self.write_json_report(results, output_dir, suite_name=suite_name)
        html_path = self.write_html_report(results, output_dir, suite_name=suite_name)
        return {"json_path": json_path, "html_path": html_path}

    def print_summary(self, results: List[Dict]) -> None:
        """Print a human-readable summary table to stdout.

        Args:
            results: List of result dicts from :meth:`run_all_tests`.
        """
        report = self._harness.generate_report(results)
        total = report["total_tests"]
        passed = report["passed"]
        failed = report["failed"]
        pass_rate = report["pass_rate"]
        summary = report["summary"]
        failed_tests = report["failed_tests"]
        by_dim = report["by_dimension"]
        by_pri = report["by_priority"]

        SEP = "=" * 48
        print(SEP)
        print("NOVA TEST SUITE RESULTS")
        print(f"{'Total Tests:':<20}{total:>10}")
        print(f"{'Passed:':<20}{passed:>10}")
        print(f"{'Failed:':<20}{failed:>10}")
        print(f"{'Pass Rate:':<20}{pass_rate:>9.1f}%")
        print(f"{'Total Tokens:':<20}{summary['total_tokens_used']:>10,}")
        print(f"{'Total Duration:':<20}{summary['total_duration_seconds']:>9.1f}s")
        print(f"{'Avg Response:':<20}{summary['average_response_time_ms'] / 1000:>9.1f}s")

        if failed_tests:
            print("\nFailed Tests:")
            print()
            for ft in failed_tests:
                print(f"  {ft['test_id']}: {ft['test_name']}")
                if ft.get("reason"):
                    print(f"    {ft['reason']}")

        print("\nBy Dimension:")
        print()
        for dim_name, data in sorted(by_dim.items(), key=lambda x: x[1]["pass_rate"]):
            label = f"  {dim_name}:"
            value = f"{data['passed']}/{data['total']} ({data['pass_rate']:.1f}%)"
            print(f"{label:<38}{value}")

        print("\nBy Priority:")
        print()
        for pri in _PRIORITY_ORDER:
            if pri not in by_pri:
                continue
            data = by_pri[pri]
            label = f"  {pri}:"
            value = f"{data['passed']}/{data['total']} ({data['pass_rate']:.1f}%)"
            print(f"{label:<38}{value}")

        print(SEP)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_template_context(self, results: List[Dict], report: Dict) -> Dict:
        """Build the Jinja2 template context dict from aggregated report data."""
        total = report["total_tests"]
        passed = report["passed"]
        failed = report["failed"]
        pass_rate = report["pass_rate"]
        summary = report["summary"]

        # by_dimension: sorted list, failures first (ascending pass_rate)
        by_dimension = sorted(
            [
                {
                    "name": name,
                    "total": data["total"],
                    "passed": data["passed"],
                    "failed": data["failed"],
                    "pass_rate": data["pass_rate"],
                }
                for name, data in report["by_dimension"].items()
            ],
            key=lambda x: x["pass_rate"],
        )

        # by_priority: fixed order Critical → High → Medium → Low
        by_priority = [
            {
                "name": pri,
                "total": report["by_priority"][pri]["total"],
                "passed": report["by_priority"][pri]["passed"],
                "failed": report["by_priority"][pri]["failed"],
                "pass_rate": report["by_priority"][pri]["pass_rate"],
            }
            for pri in _PRIORITY_ORDER
            if pri in report["by_priority"]
        ]

        # Build lookup maps for enriching failed/passed test details
        result_map = {r["test_id"]: r for r in results}
        test_suite_map = {tc["test_id"]: tc for tc in self._test_suite}

        # Enrich failed_tests with full agent response and test case fields
        failed_tests = []
        for ft in report["failed_tests"]:
            raw = result_map.get(ft["test_id"], {})
            tc = test_suite_map.get(ft["test_id"], {})
            failed_tests.append(
                {
                    # Core identifiers
                    "test_id": ft["test_id"],
                    "test_name": ft["test_name"],
                    "dimension": ft["dimension"],
                    "priority": ft["priority"],
                    "reason": ft["reason"],
                    # From raw result
                    "agent_response": raw.get("agent_response", ""),
                    "response_sample": (raw.get("response_sample") or "")[:100],
                    "grading_details": raw.get("grading_details", {}),
                    "grading_reason": raw.get("grading_reason", ft["reason"]),
                    "duration_ms": raw.get("duration_ms", 0.0),
                    "tokens_used": raw.get("tokens_used") or {},
                    "error": raw.get("error"),
                    # From test case definition
                    "prompt": tc.get("prompt", ""),
                    "expected_behavior": tc.get("expected_behavior", ""),
                    "assertion_method": tc.get("assertion_method", ""),
                    "should_contain": tc.get("should_contain") or [],
                    "should_not_contain": tc.get("should_not_contain") or [],
                    "grader_rubric": tc.get("grader_rubric", ""),
                    "test_type": tc.get("test_type", ""),
                    "requirement_ref": tc.get("requirement_ref", ""),
                    "owasp_llm": tc.get("owasp_llm", ""),
                    "regression_anchor": tc.get("regression_anchor", ""),
                    "multi_turn_setup": tc.get("multi_turn_setup", ""),
                }
            )

        # Build passed_tests list for the "Passed Tests" section
        passed_tests = []
        for r in results:
            if r["passed"]:
                tc = test_suite_map.get(r["test_id"], {})
                passed_tests.append(
                    {
                        "test_id": r["test_id"],
                        "test_name": r["test_name"],
                        "dimension": r["dimension"],
                        "priority": r["priority"],
                        "assertion_method": tc.get("assertion_method", ""),
                        "duration_ms": r.get("duration_ms", 0.0),
                        "tokens_used": r.get("tokens_used") or {},
                        "grading_reason": r.get("grading_reason", ""),
                    }
                )

        exec_date = (
            self._start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            if self._start_time
            else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        )

        return {
            "test_suite": report.get("test_suite", "Nova"),
            "execution_date": exec_date,
            "total_tests": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": pass_rate,
            "by_dimension": by_dimension,
            "by_priority": by_priority,
            "failed_tests": failed_tests,
            "passed_tests": passed_tests,
            "total_tokens": summary["total_tokens_used"],
            "total_duration_seconds": summary["total_duration_seconds"],
            "average_response_time_ms": summary["average_response_time_ms"],
            "model": self._config.model,
            "api_base_url": self._config.api_base_url,
        }

    def _safe_timestamp(self) -> str:
        """Return a filesystem-safe ISO timestamp (colons replaced with underscores)."""
        ts = (self._start_time or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H_%M_%SZ"
        )
        return ts

    @staticmethod
    def _make_error_result(test_case: Dict, error: str) -> Dict:
        """Build a failed result dict for a test case that raised an exception."""
        return {
            "test_id": test_case.get("test_id", "UNKNOWN"),
            "test_name": test_case.get("test_name", "Unknown"),
            "dimension": test_case.get("dimension", "Unknown"),
            "priority": test_case.get("priority", "Unknown"),
            "passed": False,
            "duration_ms": 0.0,
            "tokens_used": {},
            "error": error,
            "grading_reason": f"Exception during test execution: {error}",
            "grading_details": {},
            "response_sample": None,
        }
