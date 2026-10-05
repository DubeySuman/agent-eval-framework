"""HTTP client for the AgentOS / Nova API (OpenAI-compatible chat completions)."""

import logging
import time
import uuid
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Dict, Optional

import requests

from config.settings import Settings

# Status codes that indicate a transient server-side failure worth retrying
# (non-429 path — unchanged from original behaviour).
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}

# Seconds to wait before the first retry; multiplied by backoff_factor each time.
_BASE_WAIT_SECONDS = 1.0

# ── Circuit-breaker constants ─────────────────────────────────────────────────
_CB_429_BACKOFF_BASE = 2.0       # seconds: 2 → 4 → 8 → 16 (cap)
_CB_429_BACKOFF_CAP  = 16.0
_CB_TRIP_THRESHOLD   = 5         # consecutive 429s before circuit opens
_CB_OPEN_WAIT        = 60.0      # seconds to wait when circuit is OPEN


def _routing_log() -> logging.Logger:
    """Return (or create) the file-only judge_routing logger."""
    log = logging.getLogger("judge_routing")
    if not log.handlers:
        log_dir = Path("harness") / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "judge_routing.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        ))
        log.addHandler(fh)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


class CircuitBreaker:
    """Per-session circuit breaker for Nova 429 rate-limit responses.

    States
    ------
    CLOSED   Normal operation. Consecutive 429 counter increments on each 429.
    OPEN     Triggered after ``trip_threshold`` consecutive 429s. All requests
             are blocked for ``open_wait`` seconds, then transition to HALF_OPEN.
    HALF_OPEN One probe request is allowed. If it succeeds → CLOSED. If it
             429s again → back to OPEN.

    On every 429 the caller should call ``on_429(retry_after_header)`` to get
    the backoff delay it should sleep before retrying. State resets to CLOSED on
    the first non-429 success.
    """

    def __init__(
        self,
        trip_threshold: int = _CB_TRIP_THRESHOLD,
        open_wait: float = _CB_OPEN_WAIT,
        logger: Optional[Logger] = None,
    ) -> None:
        self._trip_threshold = trip_threshold
        self._open_wait = open_wait
        self._log = logger or logging.getLogger(__name__)
        self._rlog = _routing_log()

        self._state = "CLOSED"
        self._consecutive_429s = 0
        self._attempt_count = 0   # within-request 429 attempt counter

    # ── Public interface ───────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def is_open(self) -> bool:
        return self._state == "OPEN"

    def on_success(self) -> None:
        """Call after a successful (200) response."""
        if self._state in ("HALF_OPEN", "OPEN"):
            self._transition("CLOSED")
        self._consecutive_429s = 0
        self._attempt_count = 0

    def on_429(self, retry_after_header: Optional[str] = None) -> float:
        """Call when a 429 is received. Returns seconds to sleep before retry.

        Also increments the consecutive counter and trips the circuit when the
        threshold is reached. The caller is responsible for sleeping the
        returned duration and then checking ``is_open()`` / ``wait_if_open()``.
        """
        self._consecutive_429s += 1
        self._attempt_count += 1

        # Determine sleep: honour Retry-After if present, else exponential backoff
        try:
            sleep_sec = float(retry_after_header) if retry_after_header else 0.0
        except (TypeError, ValueError):
            sleep_sec = 0.0

        if sleep_sec <= 0:
            sleep_sec = min(
                _CB_429_BACKOFF_BASE * (2 ** (self._attempt_count - 1)),
                _CB_429_BACKOFF_CAP,
            )

        self._rlog.info(
            "agent_429 consecutive=%d attempt=%d backoff=%.1fs retry_after=%s",
            self._consecutive_429s, self._attempt_count, sleep_sec, retry_after_header or "none",
        )
        self._log.warning(
            "Nova 429 (consecutive=%d, attempt=%d) — backing off %.1fs",
            self._consecutive_429s, self._attempt_count, sleep_sec,
        )

        # Trip the circuit once we hit the threshold
        if self._consecutive_429s >= self._trip_threshold and self._state == "CLOSED":
            self._transition("OPEN")

        return sleep_sec

    def wait_if_open(self) -> None:
        """Block for ``open_wait`` seconds when the circuit is OPEN, then go HALF_OPEN."""
        if self._state != "OPEN":
            return
        self._log.warning(
            "CIRCUIT_OPEN — pausing all requests for %.0fs before retry", self._open_wait
        )
        self._rlog.info("circuit_state=OPEN pausing=%.0fs", self._open_wait)
        time.sleep(self._open_wait)
        self._transition("HALF_OPEN")

    def reset_attempt_count(self) -> None:
        """Reset per-request attempt counter at the start of each new test."""
        self._attempt_count = 0

    # ── Private ────────────────────────────────────────────────────────────

    def _transition(self, new_state: str) -> None:
        old = self._state
        self._state = new_state
        self._log.warning("Circuit breaker: %s → %s", old, new_state)
        self._rlog.info("circuit_state=%s→%s consecutive_429s=%d", old, new_state, self._consecutive_429s)
        if new_state == "CLOSED":
            self._consecutive_429s = 0


class GatewayHTTPClient:
    """Thin HTTP wrapper around the AgentOS gateway.

    Handles authentication headers, retries with exponential backoff,
    and normalises raw API responses into a consistent dict shape.

    All methods are safe to call from a single thread. The underlying
    requests.Session is NOT thread-safe; create one client per thread
    if parallel execution is ever introduced.
    """

    def __init__(self, config: Settings, logger: Logger) -> None:
        """Configure the client from validated settings.

        Args:
            config: Validated Settings instance.
            logger: Caller-supplied logger (avoids module-level logger coupling).
        """
        self._config = config
        self._log = logger
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.gateway_token}",
                "Content-Type": "application/json",
            }
        )
        # Circuit breaker is per-session (resets on new TestRunner run).
        self.circuit_breaker = CircuitBreaker(logger=logger)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(
        self,
        prompt: str,
        model: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Dict:
        """POST a chat completion request and return a normalised response dict.

        On transient failures (5xx, 429, connection errors, timeouts) the
        request is retried up to ``max_retries`` times with exponential backoff::

            wait = BASE_WAIT * (backoff_factor ** attempt)   # attempt starts at 0

        Args:
            prompt: User message content to send.
            model: Model identifier; defaults to ``config.model``.
            session_key: Optional thread ID for multi-turn conversations.
            timeout: Per-request timeout in seconds; defaults to ``config.timeout``.
            max_retries: Override retry cap; defaults to ``config.max_retries``.

        Returns:
            On success (HTTP 200)::

                {
                    "status_code": 200,
                    "agent_response": str,
                    "tokens_used": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
                    "duration_ms": float,
                    "session_key": str | None,
                    "request_id": str,
                    "raw_response": dict,
                }

            On non-transient HTTP error::

                {"status_code": int, "error": str, "agent_response": None, "duration_ms": float}

            On timeout or connection failure after all retries::

                {"status_code": 0, "error": str, "agent_response": None, "duration_ms": float}
        """
        effective_model = model or self._config.model
        effective_timeout = timeout if timeout is not None else self._config.timeout
        effective_retries = max_retries if max_retries is not None else self._config.max_retries
        backoff_factor = self._config.retry_backoff_factor

        url = f"{self._config.api_base_url}/chat/completions"

        body: Dict = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if session_key:
            body["x-agentos-session-key"] = session_key

        self._log.debug(
            "POST %s | model=%s | timeout=%ds | session_key=%s",
            url,
            effective_model,
            effective_timeout,
            session_key,
        )

        cb = self.circuit_breaker
        cb.reset_attempt_count()

        # If the circuit is already open from a previous test, wait it out first.
        cb.wait_if_open()

        start = datetime.now()

        for attempt in range(effective_retries + 1):
            try:
                resp = self._session.post(url, json=body, timeout=effective_timeout)
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000

                if resp.status_code == 200:
                    cb.on_success()
                    return self._parse_success(resp, elapsed_ms, session_key)

                # ── 429: circuit-breaker path ──────────────────────────────
                # Do NOT retry within the same request attempt; instead get the
                # backoff delay, sleep, check if the circuit tripped, then loop.
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_sec = cb.on_429(retry_after)

                    if cb.is_open():
                        # Circuit just tripped — block for open_wait before probe.
                        cb.wait_if_open()
                    else:
                        time.sleep(sleep_sec)

                    # Count this 429 as consuming a retry attempt.
                    if attempt >= effective_retries:
                        elapsed_ms = (datetime.now() - start).total_seconds() * 1000
                        self._log.error(
                            "429 persists after %d attempt(s) — giving up on this test",
                            attempt + 1,
                        )
                        return {
                            "status_code": 429,
                            "error": "Rate limited after all retry attempts",
                            "agent_response": None,
                            "duration_ms": elapsed_ms,
                        }
                    continue

                # ── Other transient errors: existing exponential-backoff path ──
                if resp.status_code in _TRANSIENT_STATUS_CODES and attempt < effective_retries:
                    wait = _BASE_WAIT_SECONDS * (backoff_factor ** attempt)
                    self._log.info(
                        "Transient HTTP %d on attempt %d/%d — retrying in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        effective_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable HTTP error (400, 401, 403, 404, or exhausted retries)
                error_msg = self._extract_error_message(resp)
                self._log.error(
                    "HTTP %d from %s after %d attempt(s): %s",
                    resp.status_code,
                    url,
                    attempt + 1,
                    error_msg,
                )
                return {
                    "status_code": resp.status_code,
                    "error": error_msg,
                    "agent_response": None,
                    "duration_ms": elapsed_ms,
                }

            except requests.Timeout:
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000
                if attempt < effective_retries:
                    wait = _BASE_WAIT_SECONDS * (backoff_factor ** attempt)
                    self._log.info(
                        "Request timed out on attempt %d/%d — retrying in %.1fs",
                        attempt + 1,
                        effective_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                self._log.error("Request timed out after %d attempt(s)", attempt + 1)
                return {
                    "status_code": 0,
                    "error": f"Timeout after {effective_retries} retries",
                    "agent_response": None,
                    "duration_ms": elapsed_ms,
                }

            except requests.ConnectionError as exc:
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000
                if attempt < effective_retries:
                    wait = _BASE_WAIT_SECONDS * (backoff_factor ** attempt)
                    self._log.info(
                        "Connection error on attempt %d/%d — retrying in %.1fs: %s",
                        attempt + 1,
                        effective_retries,
                        wait,
                        exc,
                    )
                    time.sleep(wait)
                    continue
                self._log.error("Connection failed after %d attempt(s): %s", attempt + 1, exc)
                return {
                    "status_code": 0,
                    "error": "Connection failed",
                    "agent_response": None,
                    "duration_ms": (datetime.now() - start).total_seconds() * 1000,
                }

            except requests.RequestException as exc:
                elapsed_ms = (datetime.now() - start).total_seconds() * 1000
                self._log.error("Unhandled request exception: %s", exc)
                return {
                    "status_code": 0,
                    "error": str(exc),
                    "agent_response": None,
                    "duration_ms": elapsed_ms,
                }

        # Unreachable in normal flow, but guards against loop logic changes.
        elapsed_ms = (datetime.now() - start).total_seconds() * 1000
        return {
            "status_code": 0,
            "error": f"Max retries ({effective_retries}) exceeded",
            "agent_response": None,
            "duration_ms": elapsed_ms,
        }

    def close(self) -> None:
        """Release the underlying connection pool. Call when done with this client."""
        self._session.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_success(
        self,
        resp: requests.Response,
        elapsed_ms: float,
        sent_session_key: Optional[str],
    ) -> Dict:
        """Decode a 200 response and return the normalised result dict."""
        try:
            data = resp.json()
        except ValueError:
            self._log.error("Failed to decode JSON from 200 response")
            return {
                "status_code": 200,
                "error": "Invalid JSON response",
                "agent_response": None,
                "duration_ms": elapsed_ms,
            }

        try:
            agent_response: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            self._log.error("Unexpected response shape — missing choices/message/content: %s", exc)
            return {
                "status_code": 200,
                "error": f"Unexpected response shape: {exc}",
                "agent_response": None,
                "duration_ms": elapsed_ms,
                "raw_response": data,
            }

        usage = data.get("usage") or {}

        # Session key: prefer response header, then body field, then echo sent value.
        session_key = (
            resp.headers.get("x-agentos-session-key")
            or data.get("x-agentos-session-key")
            or sent_session_key
        )

        request_id = resp.headers.get("x-request-id") or str(uuid.uuid4())

        self._log.info(
            "HTTP 200 | %.0fms | tokens=%s | request_id=%s",
            elapsed_ms,
            usage.get("total_tokens", "?"),
            request_id,
        )

        return {
            "status_code": 200,
            "agent_response": agent_response,
            "tokens_used": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "duration_ms": elapsed_ms,
            "session_key": session_key,
            "request_id": request_id,
            "raw_response": data,
        }

    @staticmethod
    def _extract_error_message(resp: requests.Response) -> str:
        """Pull a human-readable error string out of an error response."""
        try:
            body = resp.json()
            err = body.get("error", "")
            if isinstance(err, dict):
                return err.get("message", resp.text[:300])
            return str(err) or resp.text[:300]
        except ValueError:
            return resp.text[:300]
