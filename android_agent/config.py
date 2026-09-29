"""Environment-only settings for the clean v2 agent."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_chat_id: int
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("ANDROID_AGENT_BOT_TOKEN", "").strip()
        owner = os.environ.get("ANDROID_AGENT_OWNER_CHAT_ID", "").strip()
        base_url = os.environ.get("ANDROID_AGENT_LLM_BASE_URL", "").strip()
        model = os.environ.get("ANDROID_AGENT_LLM_MODEL", "").strip()
        missing = [
            name
            for name, value in (
                ("ANDROID_AGENT_BOT_TOKEN", token),
                ("ANDROID_AGENT_OWNER_CHAT_ID", owner),
                ("ANDROID_AGENT_LLM_BASE_URL", base_url),
                ("ANDROID_AGENT_LLM_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))
        try:
            owner_id = int(owner)
        except ValueError as exc:
            raise ValueError("ANDROID_AGENT_OWNER_CHAT_ID must be an integer") from exc
        return cls(
            telegram_bot_token=token,
            owner_chat_id=owner_id,
            llm_base_url=base_url,
            llm_model=model,
            llm_api_key=os.environ.get("ANDROID_AGENT_LLM_API_KEY") or None,
        )
