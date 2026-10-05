"""Core test harness — sends messages to the agent and evaluates responses."""

import re
from collections import defaultdict
from datetime import datetime, timezone
from logging import Logger
from typing import Dict, List, Optional

from config.settings import Settings
from harness.graders.keyword_grader import KeywordGrader
from utils.http_client import GatewayHTTPClient

# Fallback latency threshold (ms) when none is parseable from the test case.
_DEFAULT_LATENCY_THRESHOLD_MS = 5000.0


class AgentTestHarness:
    """Orchestrates individual test case execution against the Nova agent.

    Graders are injected at call time via the ``graders`` dict so this class
    has no direct dependency on optional graders (JudgeGrader, SemanticGrader)
    that require extra API keys or libraries.

    Expected ``graders`` keys:
        - ``"keyword"`` → :class:`KeywordGrader` (required for most tests)
        - ``"judge"``   → :class:`JudgeGrader`   (optional; skipped if absent)
        - ``"semantic"``→ :class:`SemanticGrader` (optional; skipped if absent)
    """

    def __init__(
        self,
        config: Settings,
        http_client: GatewayHTTPClient,
        logger: Logger,
    ) -> None:
        """Wire up the harness with its dependencies.

        Args:
            config: Validated Settings instance.
            http_client: Configured GatewayHTTPClient ready to make API calls.
            logger: Caller-supplied logger for structured output.
        """
        self._config = config
        self._http = http_client
        self._log = logger
        # Persists session keys across multi-turn tests: test_id → session_key
        self._session_keys: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, prompt: str, session_key: Optional[str] = None) -> Dict:
        """Send a single prompt to the agent via the HTTP client.

        Args:
            prompt: User message to send.
            session_key: Optional thread ID for multi-turn conversations.

        Returns:
            Normalised response dict from :meth:`GatewayHTTPClient.send_message`.
        """
        return self._http.send_message(
            prompt=prompt,
            model=self._config.model,
            session_key=session_key,
            timeout=self._config.timeout,
            max_retries=self._config.max_retries,
        )

    def run_test_case(self, test_case: Dict, graders: Dict) -> Dict:
        """Execute one test case end-to-end and return a structured result.

        Steps:
            1. If ``multi_turn_setup`` is set, send the setup turn first to
               establish a session context and capture the session key.
            2. Send the main ``prompt`` (with session key if multi-turn).
            3. On API error, return a failed result immediately.
            4. Route the agent response to the appropriate grader.
            5. Store the session key for potential downstream multi-turn use.

        Args:
            test_case: Single test case dict from :func:`tests.test_definitions.load_test_suite`.
            graders: Dict of grader instances keyed by ``"keyword"``, ``"judge"``,
                     and/or ``"semantic"``.

        Returns:
            ::

                {
                    "test_id": str,
                    "test_name": str,
                    "dimension": str,
                    "priority": str,
                    "passed": bool,
                    "duration_ms": float,
                    "tokens_used": dict,
                    "error": str | None,
                    "grading_reason": str,
                    "grading_details": dict,
                    "response_sample": str | None,   # first 200 chars
                }
        """
        test_id = test_case["test_id"]
        test_name = test_case["test_name"]
        assertion_method = test_case.get("assertion_method", "keyword_contains")
        multi_turn_setup = test_case.get("multi_turn_setup", "-").strip()

        # --- Multi-turn: send setup turn first ---
        session_key: Optional[str] = None
        if multi_turn_setup and multi_turn_setup != "-":
            self._log.debug("Multi-turn setup for %s — sending setup prompt", test_id)
            setup_resp = self.send_message(multi_turn_setup)
            if setup_resp.get("status_code") == 200:
                session_key = setup_resp.get("session_key")
                self._log.debug("Multi-turn session_key for %s: %s", test_id, session_key)
            else:
                self._log.warning(
                    "Multi-turn setup failed for %s (%s): %s",
                    test_id,
                    setup_resp.get("status_code"),
                    setup_resp.get("error"),
                )

        # --- Send main prompt ---
        response = self.send_message(test_case["prompt"], session_key=session_key)

        # Persist session key for downstream multi-turn tests in the same group
        if response.get("session_key"):
            self._session_keys[test_id] = response["session_key"]

        duration_ms: float = response.get("duration_ms", 0.0)
        tokens_used: Dict = response.get("tokens_used") or {}

        # --- API-level error ---
        if response.get("status_code") != 200:
            self._log.error(
                "FAIL %s | API error %s: %s",
                test_id,
                response.get("status_code"),
                response.get("error"),
            )
            return self._build_result(
                test_case=test_case,
                passed=False,
                duration_ms=duration_ms,
                tokens_used=tokens_used,
                error=response.get("error", "API request failed"),
                grading_reason=f"API error {response.get('status_code')}: {response.get('error', 'unknown')}",
                grading_details={},
                response_sample=None,
            )

        agent_response: str = response["agent_response"]
        response_sample = (agent_response or "")[:200]

        # --- Grade ---
        grade = self._grade_response(
            test_case=test_case,
            agent_response=agent_response,
            duration_ms=duration_ms,
            assertion_method=assertion_method,
            graders=graders,
        )

        status_label = "PASS" if grade["passed"] else "FAIL"
        self._log.info(
            "%s %s | %s | %.0fms | %s",
            status_label,
            test_id,
            test_name,
            duration_ms,
            grade.get("reason", ""),
        )

        return self._build_result(
            test_case=test_case,
            passed=grade["passed"],
            duration_ms=duration_ms,
            tokens_used=tokens_used,
            error=None,
            grading_reason=grade.get("reason", ""),
            grading_details=grade.get("details", {}),
            response_sample=response_sample,
            agent_response=agent_response,
        )

    def generate_report(self, results: List[Dict]) -> Dict:
        """Aggregate a list of test results into a structured summary report.

        Args:
            results: List of dicts returned by :meth:`run_test_case`.

        Returns:
            Report dict containing totals, dimension/priority breakdowns,
            failed test details, and execution summary statistics.
        """
        total = len(results)
        passed_count = sum(1 for r in results if r["passed"])
        failed_count = total - passed_count
        pass_rate = round((passed_count / total * 100) if total else 0.0, 2)

        # Dimension breakdown
        by_dim: Dict[str, Dict] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
        for r in results:
            g = by_dim[r["dimension"]]
            g["total"] += 1
            if r["passed"]:
                g["passed"] += 1
            else:
                g["failed"] += 1
        for g in by_dim.values():
            g["pass_rate"] = round((g["passed"] / g["total"] * 100) if g["total"] else 0.0, 2)

        # Priority breakdown
        by_pri: Dict[str, Dict] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
        for r in results:
            g = by_pri[r["priority"]]
            g["total"] += 1
            if r["passed"]:
                g["passed"] += 1
            else:
                g["failed"] += 1
        for g in by_pri.values():
            g["pass_rate"] = round((g["passed"] / g["total"] * 100) if g["total"] else 0.0, 2)

        # Failed test details
        failed_tests = [
            {
                "test_id": r["test_id"],
                "test_name": r["test_name"],
                "dimension": r["dimension"],
                "priority": r["priority"],
                "reason": r["grading_reason"],
                "details": r["grading_details"],
            }
            for r in results
            if not r["passed"]
        ]

        # Execution stats
        total_tokens = sum(
            (r.get("tokens_used") or {}).get("total_tokens", 0) for r in results
        )
        durations = [r["duration_ms"] for r in results if r.get("duration_ms") is not None]
        total_duration_s = round(sum(durations) / 1000, 2)
        avg_response_ms = round(sum(durations) / len(durations), 2) if durations else 0.0

        return {
            "test_suite": f"Nova Test Suite ({total} tests)",
            "execution_date": datetime.now(timezone.utc).isoformat(),
            "total_tests": total,
            "passed": passed_count,
            "failed": failed_count,
            "pass_rate": pass_rate,
            "by_dimension": dict(by_dim),
            "by_priority": dict(by_pri),
            "failed_tests": failed_tests,
            "summary": {
                "total_tokens_used": total_tokens,
                "total_duration_seconds": total_duration_s,
                "average_response_time_ms": avg_response_ms,
            },
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _grade_response(
        self,
        test_case: Dict,
        agent_response: str,
        duration_ms: float,
        assertion_method: str,
        graders: Dict,
    ) -> Dict:
        """Route all tests to JudgeGrader using intent + judge_rubric.

        ``assertion_method`` is accepted but ignored — all tests are now evaluated
        by the LLM judge. Returns a dict with ``passed``, ``reason``, and ``details``.
        """
        judge_grader = graders.get("judge")
        if judge_grader is None:
            self._log.warning(
                "JudgeGrader not configured — skipping %s", test_case["test_id"]
            )
            return {"passed": True, "reason": "JudgeGrader not configured — test skipped", "details": {}}

        result = judge_grader.grade(
            response_text=agent_response,
            intent=test_case.get("intent", ""),
            judge_rubric=test_case.get("judge_rubric", ""),
            test_name=test_case.get("test_name", ""),
            test_id=test_case.get("test_id", ""),
        )
        return {
            "passed": result["passed"],
            "reason": result.get("reasoning", ""),
            "details": {"score": result.get("score", 0)},
        }

    @staticmethod
    def _parse_latency_threshold(test_case: Dict) -> float:
        """Extract a latency threshold in ms from the test case fields.

        Searches ``grader_rubric`` then ``expected_behavior`` for patterns like
        ``2000ms``, ``2 seconds``, ``5s``. Falls back to
        ``_DEFAULT_LATENCY_THRESHOLD_MS`` if nothing is found.
        """
        pattern = re.compile(
            r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|seconds?)", re.IGNORECASE
        )
        for field in ("grader_rubric", "expected_behavior"):
            text = test_case.get(field, "")
            m = pattern.search(text)
            if m:
                value, unit = float(m.group(1)), m.group(2).lower()
                if unit.startswith("ms") or unit.startswith("milli"):
                    return value
                return value * 1000  # seconds → ms
        return _DEFAULT_LATENCY_THRESHOLD_MS

    @staticmethod
    def _build_result(
        test_case: Dict,
        passed: bool,
        duration_ms: float,
        tokens_used: Dict,
        error: Optional[str],
        grading_reason: str,
        grading_details: Dict,
        response_sample: Optional[str],
        agent_response: Optional[str] = None,
    ) -> Dict:
        """Assemble the standard result dict returned by run_test_case."""
        return {
            "test_id": test_case["test_id"],
            "test_name": test_case["test_name"],
            "dimension": test_case["dimension"],
            "priority": test_case["priority"],
            "passed": passed,
            "duration_ms": duration_ms,
            "tokens_used": tokens_used,
            "error": error,
            "grading_reason": grading_reason,
            "grading_details": grading_details,
            "response_sample": response_sample,
            "agent_response": agent_response or "",
        }
