"""Configuration loader — reads env vars and validates required settings at startup."""

import logging
import os
import re
from pathlib import Path
from typing import Dict
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

# agents.yaml lives alongside this file.
_AGENTS_YAML = Path(__file__).parent / "agents.yaml"

logger = logging.getLogger(__name__)

_REQUIRED = ("GATEWAY_TOKEN", "API_BASE_URL", "AGENT_MODEL")


class Settings:
    """Load and validate all runtime configuration from environment variables.

    Required vars: GATEWAY_TOKEN, API_BASE_URL, AGENT_MODEL.
    Optional vars default to safe values when absent.
    Raises ValueError on startup if any required var is missing or invalid.
    """

    def __init__(self, env_file: str = ".env") -> None:
        """Load .env file and validate all required settings.

        Args:
            env_file: Path to the .env file. Silently skipped if not found
                      (env vars may already be set in the process environment).

        Raises:
            ValueError: If required environment variables are missing or invalid.
        """
        load_dotenv(env_file, override=False)
        self._load()
        self._validate()
        self._agents = self._load_agents()
        self._log_summary()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Read all env vars into private attributes."""
        self._gateway_token = os.environ.get("GATEWAY_TOKEN", "")
        self._api_base_url = os.environ.get("API_BASE_URL", "")
        self._model = os.environ.get("AGENT_MODEL", "agentos/nova")
        self._claude_api_key = os.environ.get("CLAUDE_API_KEY", "")
        self._deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self._judge_model = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5-20251001")
        self._log_level = os.environ.get("LOG_LEVEL", "INFO")

        try:
            self._deepseek_delay = float(os.environ.get("DEEPSEEK_DELAY_SECONDS", "0.5"))
        except ValueError:
            raise ValueError("DEEPSEEK_DELAY_SECONDS must be a float")

        try:
            self._haiku_delay = float(os.environ.get("HAIKU_DELAY_SECONDS", "0.2"))
        except ValueError:
            raise ValueError("HAIKU_DELAY_SECONDS must be a float")
        self._results_dir = os.environ.get("RESULTS_DIR", "./results")
        self._logs_dir = os.environ.get("LOGS_DIR", "./logs")

        try:
            self._timeout = int(os.environ.get("TEST_TIMEOUT_SECONDS", "60"))
        except ValueError:
            raise ValueError("TEST_TIMEOUT_SECONDS must be an integer")

        try:
            self._max_retries = int(os.environ.get("MAX_RETRIES", "3"))
        except ValueError:
            raise ValueError("MAX_RETRIES must be an integer")

        try:
            self._retry_backoff_factor = float(os.environ.get("RETRY_BACKOFF_FACTOR", "2.0"))
        except ValueError:
            raise ValueError("RETRY_BACKOFF_FACTOR must be a float")

    def _validate(self) -> None:
        """Assert required fields are non-empty and structurally valid.

        Raises:
            ValueError: On any validation failure.
        """
        missing = [v for v in _REQUIRED if not os.environ.get(v)]
        if missing:
            raise ValueError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill in your credentials."
            )

        if len(self._gateway_token) == 0:
            raise ValueError("GATEWAY_TOKEN must not be empty")

        parsed = urlparse(self._api_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"API_BASE_URL is not a valid HTTP/HTTPS URL: '{self._api_base_url}'"
            )

        if self._timeout <= 0:
            raise ValueError("TEST_TIMEOUT_SECONDS must be > 0")

        if self._max_retries < 0:
            raise ValueError("MAX_RETRIES must be >= 0")

    def _load_agents(self) -> Dict[str, Dict[str, str]]:
        """Load and parse config/agents.yaml.

        Expands ``${VAR}`` placeholders using the current environment so
        endpoints and model IDs can reference env vars set in .env.

        Returns an empty dict (with a warning) if agents.yaml is absent,
        so existing Nova-only usage remains fully backward compatible.

        Raises:
            ValueError: If agents.yaml exists but cannot be parsed or is
                        structurally invalid.
        """
        if not _AGENTS_YAML.exists():
            logger.warning(
                "agents.yaml not found at %s — multi-agent config unavailable", _AGENTS_YAML
            )
            return {}

        try:
            raw = _AGENTS_YAML.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Cannot read agents.yaml: {exc}") from exc

        # Expand ${VAR} placeholders before YAML parsing so values referencing
        # env vars (e.g. endpoints) are resolved correctly.
        def _expand(text: str) -> str:
            return re.sub(
                r"\$\{([^}]+)\}",
                lambda m: os.environ.get(m.group(1), m.group(0)),
                text,
            )

        try:
            data = yaml.safe_load(_expand(raw))
        except yaml.YAMLError as exc:
            raise ValueError(f"agents.yaml parse error: {exc}") from exc

        if not isinstance(data, dict) or "agents" not in data:
            raise ValueError("agents.yaml must contain a top-level 'agents' mapping")

        agents_raw = data["agents"]
        if not isinstance(agents_raw, dict):
            raise ValueError("agents.yaml: 'agents' must be a mapping of agent entries")

        result: Dict[str, Dict[str, str]] = {}
        for agent_key, entry in agents_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"agents.yaml: entry '{agent_key}' must be a mapping")
            for required_field in ("endpoint", "model"):
                if required_field not in entry:
                    raise ValueError(
                        f"agents.yaml: entry '{agent_key}' is missing required field '{required_field}'"
                    )
            result[agent_key] = {
                "name":     str(entry.get("name", agent_key)),
                "endpoint": str(entry["endpoint"]),
                "model":    str(entry["model"]),
            }

        return result

    def _log_summary(self) -> None:
        """Log a sanitized config summary (token and API key values redacted)."""
        token_preview = f"{self._gateway_token[:6]}…" if self._gateway_token else "(not set)"
        key_preview = f"{self._claude_api_key[:6]}…" if self._claude_api_key else "(not set)"
        logger.info(
            "Settings loaded | url=%s | model=%s | token=%s | claude_key=%s | "
            "timeout=%ds | retries=%d | backoff=%.1f",
            self._api_base_url,
            self._model,
            token_preview,
            key_preview,
            self._timeout,
            self._max_retries,
            self._retry_backoff_factor,
        )
        if self._agents:
            logger.info("Agents loaded: %s", ", ".join(sorted(self._agents)))

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def gateway_token(self) -> str:
        """AgentOS gateway authentication token."""
        return self._gateway_token

    @property
    def api_base_url(self) -> str:
        """Base URL for the AgentOS/Nova API (no trailing slash)."""
        return self._api_base_url.rstrip("/")

    @property
    def model(self) -> str:
        """Model identifier to use for Nova requests."""
        return self._model

    @property
    def claude_api_key(self) -> str:
        """Anthropic API key for the LLM judge grader (may be empty)."""
        return self._claude_api_key

    @property
    def deepseek_api_key(self) -> str:
        """DeepSeek API key for the primary judge model (may be empty)."""
        return self._deepseek_api_key

    @property
    def judge_model(self) -> str:
        """Claude model ID used by the LLM judge grader."""
        return self._judge_model

    @property
    def deepseek_delay(self) -> float:
        """Seconds to sleep between DeepSeek API requests."""
        return self._deepseek_delay

    @property
    def haiku_delay(self) -> float:
        """Seconds to sleep between Haiku API requests."""
        return self._haiku_delay

    @property
    def timeout(self) -> int:
        """Per-request timeout in seconds."""
        return self._timeout

    @property
    def max_retries(self) -> int:
        """Maximum retry attempts for failed requests."""
        return self._max_retries

    @property
    def retry_backoff_factor(self) -> float:
        """Exponential backoff multiplier between retries."""
        return self._retry_backoff_factor

    @property
    def results_dir(self) -> str:
        """Directory where JSON/HTML reports are written."""
        return self._results_dir

    @property
    def logs_dir(self) -> str:
        """Directory where log files are written."""
        return self._logs_dir

    @property
    def log_level(self) -> str:
        """Logging level string (DEBUG, INFO, WARNING, ERROR)."""
        return self._log_level

    @property
    def agents(self) -> Dict[str, Dict[str, str]]:
        """All agents loaded from agents.yaml as ``{agent_key: {name, endpoint, model}}``."""
        return dict(self._agents)

    def get_agent_config(self, agent_name: str) -> Dict[str, str]:
        """Return ``{name, endpoint, model}`` for the given agent key.

        Args:
            agent_name: Key from agents.yaml (e.g. ``'nova'``, ``'mira'``).

        Raises:
            ValueError: If ``agent_name`` is not present in agents.yaml.
        """
        if agent_name not in self._agents:
            available = sorted(self._agents) or ["(none — agents.yaml not loaded)"]
            raise ValueError(
                f"Unknown agent '{agent_name}'. "
                f"Available agents: {', '.join(available)}"
            )
        return dict(self._agents[agent_name])
