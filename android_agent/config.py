"""Validated, environment-only settings for the v2 agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from android_agent.models.compat import Dialect

#: Endpoints that are easy to get wrong, keyed by a friendly provider name.
KNOWN_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Populate os.environ from a .env file without overwriting real values.

    Kept dependency-free on purpose: Termux installs should not need extra
    wheels just to read five variables.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return
    for line in candidate.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_chat_id: int
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    llm_dialect: Dialect
    log_level: str = "INFO"
    request_timeout_seconds: float = 60.0
    session_ttl_seconds: float = 900.0
    session_max_messages: int = 60
    state_dir: str = "~/telegram_agent_v2"
    needle_enabled: bool = False
    needle_confidence_threshold: float = 0.85
    #: Email connector. Optional: absent settings simply mean no email tools.
    #: Use a dedicated mailbox - an app password cannot be scoped, so this
    #: credential can read and delete the whole account it belongs to.
    email_address: str = ""
    email_app_password: str = ""
    email_imap_host: str = "imap.gmail.com"
    email_imap_port: int = 993
    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 465

    @property
    def email_enabled(self) -> bool:
        return bool(self.email_address and self.email_app_password)

    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> Settings:
        """Read and validate settings from the environment.

        ``require_telegram`` is False for front ends that do not talk to
        Telegram (the local web console), so a web-only install does not have
        to invent a bot token it will never use.
        """
        load_dotenv()

        token = os.environ.get("ANDROID_AGENT_BOT_TOKEN", "").strip()
        owner = os.environ.get("ANDROID_AGENT_OWNER_CHAT_ID", "").strip()
        base_url = os.environ.get("ANDROID_AGENT_LLM_BASE_URL", "").strip()
        model = os.environ.get("ANDROID_AGENT_LLM_MODEL", "").strip()

        # Convenience: allow a provider alias instead of a full URL.
        if base_url.lower() in KNOWN_ENDPOINTS:
            base_url = KNOWN_ENDPOINTS[base_url.lower()]

        required: list[tuple[str, str]] = [
            ("ANDROID_AGENT_OWNER_CHAT_ID", owner),
            ("ANDROID_AGENT_LLM_BASE_URL", base_url),
            ("ANDROID_AGENT_LLM_MODEL", model),
        ]
        if require_telegram:
            required.insert(0, ("ANDROID_AGENT_BOT_TOKEN", token))
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))

        try:
            owner_id = int(owner)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_OWNER_CHAT_ID must be an integer") from exc

        configured_dialect = os.environ.get("ANDROID_AGENT_LLM_DIALECT", "").strip().lower()
        if configured_dialect:
            try:
                dialect = Dialect(configured_dialect)
            except ValueError as exc:
                valid = ", ".join(item.value for item in Dialect)
                raise ValueError(
                    f"ANDROID_AGENT_LLM_DIALECT must be one of: {valid}"
                ) from exc
        else:
            dialect = Dialect.detect(base_url)

        timeout_raw = os.environ.get("ANDROID_AGENT_LLM_TIMEOUT", "60").strip()
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_LLM_TIMEOUT must be a number") from exc
        if timeout <= 0:
            raise ValueError("ANDROID_AGENT_LLM_TIMEOUT must be positive")

        ttl_raw = os.environ.get("ANDROID_AGENT_SESSION_TTL_MINUTES", "15").strip()
        try:
            ttl_minutes = float(ttl_raw)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_SESSION_TTL_MINUTES must be a number") from exc
        if ttl_minutes <= 0:
            raise ValueError("ANDROID_AGENT_SESSION_TTL_MINUTES must be positive")

        max_messages_raw = os.environ.get("ANDROID_AGENT_SESSION_MAX_MESSAGES", "60").strip()
        try:
            max_messages = int(max_messages_raw)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_SESSION_MAX_MESSAGES must be an integer") from exc
        if max_messages < 2:
            raise ValueError("ANDROID_AGENT_SESSION_MAX_MESSAGES must be at least 2")

        threshold_raw = os.environ.get("ANDROID_AGENT_NEEDLE_THRESHOLD", "0.85").strip()
        try:
            needle_threshold = float(threshold_raw)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_NEEDLE_THRESHOLD must be a number") from exc
        if not 0.0 < needle_threshold <= 1.0:
            raise ValueError("ANDROID_AGENT_NEEDLE_THRESHOLD must be between 0 and 1")

        email_address = (os.environ.get("ANDROID_AGENT_EMAIL_ADDRESS") or "").strip()
        email_password = (os.environ.get("ANDROID_AGENT_EMAIL_APP_PASSWORD") or "").strip()
        # Half-configured email is a mistake worth catching at startup rather
        # than when the owner first asks about their inbox.
        if bool(email_address) != bool(email_password):
            raise ValueError(
                "Set both ANDROID_AGENT_EMAIL_ADDRESS and "
                "ANDROID_AGENT_EMAIL_APP_PASSWORD, or neither"
            )
        if email_address and "@" not in email_address:
            raise ValueError("ANDROID_AGENT_EMAIL_ADDRESS must be an email address")

        def _port(name: str, default: int) -> int:
            raw = (os.environ.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if not 1 <= value <= 65535:
                raise ValueError(f"{name} must be a valid port")
            return value

        api_key = (os.environ.get("ANDROID_AGENT_LLM_API_KEY") or "").strip() or None
        if dialect is Dialect.GEMINI and not api_key:
            raise ValueError(
                "ANDROID_AGENT_LLM_API_KEY is required for the Gemini endpoint"
            )

        return cls(
            telegram_bot_token=token,
            owner_chat_id=owner_id,
            llm_base_url=base_url,
            llm_model=model,
            llm_api_key=api_key,
            llm_dialect=dialect,
            log_level=os.environ.get("ANDROID_AGENT_LOG_LEVEL", "INFO").strip() or "INFO",
            request_timeout_seconds=timeout,
            session_ttl_seconds=ttl_minutes * 60.0,
            session_max_messages=max_messages,
            state_dir=os.environ.get("ANDROID_AGENT_STATE_DIR", "~/telegram_agent_v2").strip()
            or "~/telegram_agent_v2",
            needle_enabled=os.environ.get("ANDROID_AGENT_NEEDLE", "").strip().lower()
            in {"1", "true", "yes", "on"},
            needle_confidence_threshold=needle_threshold,
            email_address=email_address,
            email_app_password=email_password,
            email_imap_host=(os.environ.get("ANDROID_AGENT_EMAIL_IMAP_HOST") or "imap.gmail.com").strip(),
            email_imap_port=_port("ANDROID_AGENT_EMAIL_IMAP_PORT", 993),
            email_smtp_host=(os.environ.get("ANDROID_AGENT_EMAIL_SMTP_HOST") or "smtp.gmail.com").strip(),
            email_smtp_port=_port("ANDROID_AGENT_EMAIL_SMTP_PORT", 465),
        )

    def redacted(self) -> dict[str, object]:
        """A safe-to-print view of the configuration."""
        return {
            "owner_chat_id": self.owner_chat_id,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_dialect": self.llm_dialect.value,
            "llm_api_key": "set" if self.llm_api_key else "absent",
            "telegram_bot_token": "set" if self.telegram_bot_token else "absent",
            "log_level": self.log_level,
            "session_ttl_minutes": round(self.session_ttl_seconds / 60, 1),
            "state_dir": self.state_dir,
            "needle_fast_path": self.needle_enabled,
            # The address is useful in a log; the app password never is.
            "email_address": self.email_address or "absent",
            "email_app_password": "set" if self.email_app_password else "absent",
        }
