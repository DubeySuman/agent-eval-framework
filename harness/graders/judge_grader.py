"""LLM-as-judge grader — DeepSeek primary, Claude Haiku fallback (per-test)."""

import hashlib
import json
import logging
import os
import re
import time
from logging import Logger
from pathlib import Path
from typing import Dict, Optional

import requests

# ── Models & endpoints ────────────────────────────────────────────────────────
_DEEPSEEK_MODEL = "deepseek-v4-flash"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# ── Retry / rate-limit config ─────────────────────────────────────────────────
_MAX_DS_ATTEMPTS = 3
_TIMEOUT_SECS = 30
_BACKOFF_BASE = 0.25   # 250 ms base for exponential backoff on 429/503
_BACKOFF_CAP = 8.0     # cap at 8 s
_MAX_TOKENS = 500

# Configurable inter-request delays (override via env vars)
_DEEPSEEK_DELAY = float(os.getenv("DEEPSEEK_DELAY_SECONDS", "0.5"))
_HAIKU_DELAY = float(os.getenv("HAIKU_DELAY_SECONDS", "0.2"))

# ── Prompts ───────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are a test grader. Evaluate responses strictly against rubrics. "
    "Output only valid JSON with no markdown, no preamble, no explanation outside the JSON."
)

# Template uses double-braces to produce literal {} in the output after .format()
_JUDGE_PROMPT_TEMPLATE = """\
You are a test judge evaluating an AI agent's response.

TEST INTENT: {intent}

EVALUATION RUBRIC: {judge_rubric}

AGENT RESPONSE:
{response_text}

Score the response 0-3 based on the rubric above.
Respond with ONLY valid JSON:
{{
  "score": <0, 1, 2, or 3>,
  "reasoning": "Why this score? (1-2 sentences)",
  "passed": <true if score >= 2, false if score <= 1>
}}

Only output the JSON, no preamble."""

_CACHE_FILENAME = "judge_cache.json"

_THROTTLE_STATUSES = (429, 503)


def _setup_routing_logger() -> logging.Logger:
    """Create a file-only logger that writes to harness/logs/judge_routing.log."""
    log = logging.getLogger("judge_routing")
    if log.handlers:
        return log
    log_dir = Path("harness") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / "judge_routing.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


class JudgeGrader:
    """Evaluate agent responses using DeepSeek V4 Flash (primary) with Claude Haiku fallback.

    Routing is per-test:
      1. Try DeepSeek up to 3 times (with exponential backoff on 429/503).
      2. If all DeepSeek attempts fail, use Haiku for that test only.
      3. The next test starts fresh with DeepSeek — fallback never persists.

    Results are cached by SHA-256(response + rubric + intent) so repeated runs
    avoid redundant API calls regardless of which model produced the result.

    When DEEPSEEK_API_KEY is absent, runs in Haiku-only mode.
    """

    def __init__(
        self,
        claude_api_key: str,
        model: str = _HAIKU_MODEL,
        logger: Optional[Logger] = None,
        cache_dir: str = "./logs",
        judge_timeout: int = _TIMEOUT_SECS,
    ) -> None:
        """Configure the judge grader.

        Args:
            claude_api_key: Anthropic API key for Haiku fallback.
            model: Unused — kept for backward-compat; routing decides the model.
            logger: Optional caller-supplied logger.
            cache_dir: Directory for ``judge_cache.json``.
            judge_timeout: Per-request timeout in seconds.
        """
        self._haiku_key = claude_api_key
        self._deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._deepseek_available = bool(self._deepseek_key)
        self._log = logger or logging.getLogger(__name__)
        self._cache_dir = cache_dir
        self._timeout = judge_timeout
        self._cache: Dict[str, Dict] = {}
        self._routing_log = _setup_routing_logger()

        self.stats: Dict = {
            "total_grades": 0,
            "cache_hits": 0,
            "deepseek_calls": 0,
            "haiku_calls": 0,
            "fallbacks": 0,
            "errors": 0,
            "average_score": 0.0,
        }

        if not self._deepseek_available:
            self._log.warning(
                "DEEPSEEK_API_KEY not set — JudgeGrader running in Haiku-only mode"
            )

        self.load_cache()

    # ── Public API ─────────────────────────────────────────────────────────────

    def grade(
        self,
        response_text: str,
        judge_rubric: str = "",
        test_name: str = "",
        intent: str = "",
        test_id: str = "",
    ) -> Dict:
        """Score ``response_text`` 0-3 via DeepSeek primary / Haiku fallback.

        Score 0-1 → passed=False, score 2-3 → passed=True.
        Cache is checked first; API is only called on a miss.

        Args:
            response_text: Raw text returned by the agent under test.
            judge_rubric: Combined PASS/FAIL criteria + 0-3 scoring guidance.
            test_name: Human-readable name for log lines.
            intent: What the test is checking for.
            test_id: Short identifier used in routing logs (falls back to test_name).

        Returns:
            ``{"passed": bool, "score": int, "reasoning": str}``
        """
        self.stats["total_grades"] += 1
        label = test_id or test_name or "unknown"

        cache_key = self._make_cache_key(response_text, judge_rubric, intent)
        if cache_key in self._cache:
            self.stats["cache_hits"] += 1
            self._log.debug("Cache hit for '%s'", label)
            result = dict(self._cache[cache_key])
            self._update_avg_score(result.get("score", 0))
            return result

        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            intent=intent or "(no intent specified)",
            judge_rubric=judge_rubric or "(no rubric specified)",
            response_text=response_text,
        )

        result = self._grade_with_routing(prompt, label)

        self._cache[cache_key] = result
        self.save_cache()
        self._update_avg_score(result.get("score", 0))

        verdict = "PASS" if result["passed"] else "FAIL"
        self._log.info(
            "Graded '%s': %s (score: %d) — %s",
            label, verdict, result["score"], result["reasoning"],
        )
        return result

    def get_stats(self) -> Dict:
        """Return a copy of accumulated grading statistics."""
        return dict(self.stats)

    # ── Cache persistence ──────────────────────────────────────────────────────

    def load_cache(self) -> None:
        """Load persisted cache from disk; silently skips on first run."""
        path = os.path.join(self._cache_dir, _CACHE_FILENAME)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                self._cache = json.load(fh)
            self._log.debug("Loaded %d cached results from %s", len(self._cache), path)
        except (json.JSONDecodeError, OSError) as exc:
            self._log.warning("Could not load judge cache (%s) — starting fresh", exc)
            self._cache = {}

    def save_cache(self) -> None:
        """Write in-memory cache to disk; logs a warning on failure, never raises."""
        path = os.path.join(self._cache_dir, _CACHE_FILENAME)
        try:
            Path(self._cache_dir).mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, indent=2)
        except OSError as exc:
            self._log.warning("Could not save judge cache: %s", exc)

    # ── Routing ────────────────────────────────────────────────────────────────

    def _grade_with_routing(self, prompt: str, label: str) -> Dict:
        """Try DeepSeek 3×, fall back to Haiku if all attempts fail (per-test).

        The fallback is strictly per-test: the next call to this method always
        starts with DeepSeek regardless of what happened here.
        """
        if self._deepseek_available:
            for attempt in range(1, _MAX_DS_ATTEMPTS + 1):
                t0 = time.monotonic()
                outcome = self._call_deepseek(prompt, label)
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.stats["deepseek_calls"] += 1

                time.sleep(_DEEPSEEK_DELAY)

                if outcome["success"]:
                    self._routing_log.info(
                        "test=%s model=deepseek-v4-flash attempt=%d ms=%.0f score=%d",
                        label, attempt, elapsed_ms, outcome["result"].get("score", 0),
                    )
                    return outcome["result"]

                self._log.warning(
                    "%s: DeepSeek attempt %d/%d failed — %s",
                    label, attempt, _MAX_DS_ATTEMPTS, outcome["error"],
                )

                # Extra sleep for throttle errors before next attempt
                if outcome.get("is_throttle") and attempt < _MAX_DS_ATTEMPTS:
                    backoff = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP)
                    retry_after = outcome.get("retry_after", 0.0)
                    time.sleep(max(backoff, retry_after))

            self.stats["fallbacks"] += 1
            self._log.warning("%s: All DeepSeek attempts failed — falling back to Haiku", label)
            self._routing_log.info(
                "test=%s DeepSeek_failed=3 falling_back=haiku", label
            )

        # Haiku path (fallback or sole provider when DEEPSEEK_API_KEY absent)
        t0 = time.monotonic()
        outcome = self._call_haiku(prompt, label)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.stats["haiku_calls"] += 1

        time.sleep(_HAIKU_DELAY)

        if outcome["success"]:
            self._routing_log.info(
                "test=%s model=claude-haiku-4-5-20251001 attempt=1 ms=%.0f score=%d",
                label, elapsed_ms, outcome["result"].get("score", 0),
            )
            return outcome["result"]

        # Both models failed
        self.stats["errors"] += 1
        self._log.error("%s: Both DeepSeek and Haiku failed — marking INCONCLUSIVE", label)
        self._routing_log.info("test=%s model=INCONCLUSIVE", label)
        return {
            "passed": False,
            "score": 0,
            "reasoning": "Judge unavailable — both models failed",
        }

    # ── DeepSeek caller ────────────────────────────────────────────────────────

    def _call_deepseek(self, prompt: str, label: str) -> Dict:
        """Single DeepSeek attempt.

        Returns ``{"success": bool, "result": dict, "error": str,
        "is_throttle": bool, "retry_after": float}``.
        """
        headers = {
            "Authorization": f"Bearer {self._deepseek_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": _DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": _MAX_TOKENS,
        }

        try:
            resp = requests.post(
                _DEEPSEEK_API_URL, headers=headers, json=body, timeout=self._timeout
            )
        except requests.Timeout:
            return {"success": False, "error": f"timeout after {self._timeout}s", "is_throttle": False}
        except (requests.ConnectionError, requests.RequestException) as exc:
            return {"success": False, "error": str(exc), "is_throttle": False}

        if resp.status_code in _THROTTLE_STATUSES:
            retry_after = 0.0
            try:
                retry_after = float(resp.headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                pass
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}",
                "is_throttle": True,
                "retry_after": retry_after,
            }

        if resp.status_code in (401, 403):
            self._log.error("DeepSeek auth failed — check DEEPSEEK_API_KEY (HTTP %d)", resp.status_code)
            return {"success": False, "error": f"auth failed (HTTP {resp.status_code})", "is_throttle": False}

        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}", "is_throttle": False}

        try:
            raw_text: str = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            return {"success": False, "error": f"response parse error: {exc}", "is_throttle": False}

        parsed = self._parse_judge_json(raw_text, label)
        if parsed is None:
            return {"success": False, "error": "invalid JSON in judge response", "is_throttle": False}

        return {"success": True, "result": parsed}

    # ── Haiku caller ───────────────────────────────────────────────────────────

    def _call_haiku(self, prompt: str, label: str) -> Dict:
        """Single Haiku attempt with system-prompt caching.

        Returns ``{"success": bool, "result": dict, "error": str}``.
        """
        headers = {
            "x-api-key": self._haiku_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31",
            "content-type": "application/json",
        }
        body = {
            "model": _HAIKU_MODEL,
            "max_tokens": _MAX_TOKENS,
            # System as a list to support cache_control
            "system": [
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            resp = requests.post(
                _ANTHROPIC_API_URL, headers=headers, json=body, timeout=self._timeout
            )
        except requests.Timeout:
            return {"success": False, "error": f"timeout after {self._timeout}s"}
        except (requests.ConnectionError, requests.RequestException) as exc:
            return {"success": False, "error": str(exc)}

        if resp.status_code in (401, 403):
            self._log.error("Haiku auth failed — check CLAUDE_API_KEY (HTTP %d)", resp.status_code)
            return {"success": False, "error": f"auth failed (HTTP {resp.status_code})"}

        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}"}

        try:
            raw_text: str = resp.json()["content"][0]["text"]
        except (ValueError, KeyError, IndexError) as exc:
            return {"success": False, "error": f"response parse error: {exc}"}

        parsed = self._parse_judge_json(raw_text, label)
        if parsed is None:
            return {"success": False, "error": "invalid JSON in judge response"}

        return {"success": True, "result": parsed}

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _parse_judge_json(self, raw_text: str, label: str) -> Optional[Dict]:
        """Parse score/reasoning/passed from the judge's raw reply.

        Returns ``None`` on parse failure so the caller can retry.
        Strips markdown code fences before parsing.
        """
        cleaned = re.sub(r"```(?:json)?\s*", "", raw_text).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            self._log.error("Judge returned invalid JSON for '%s': %s", label, raw_text[:300])
            return None

        try:
            score = max(0, min(3, int(data.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        passed = score >= 2
        reasoning = str(data.get("reasoning", "No reasoning provided")).strip()

        return {"passed": passed, "score": score, "reasoning": reasoning}

    @staticmethod
    def _make_cache_key(response_text: str, judge_rubric: str, intent: str = "") -> str:
        """SHA-256 of (response_text + judge_rubric + intent) as hex."""
        payload = (response_text + judge_rubric + intent).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _update_avg_score(self, score: int) -> None:
        """Update the running average score (Welford's incremental formula)."""
        n = self.stats["total_grades"]
        old = self.stats["average_score"]
        self.stats["average_score"] = round(old + (score - old) / n, 4)
