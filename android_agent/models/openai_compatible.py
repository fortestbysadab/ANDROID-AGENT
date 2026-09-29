"""Minimal OpenAI-compatible chat-completions planner adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping, Sequence

from .base import PlannerResponse, ToolCall


class PlannerAPIError(RuntimeError):
    pass


class OpenAICompatiblePlanner:
    """Works with services implementing the chat-completions tool API.

    Credentials stay in HTTP headers and are never included in model context.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> PlannerResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            raise PlannerAPIError(f"planner HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PlannerAPIError(f"planner request failed: {exc}") from exc

        try:
            envelope = json.loads(raw)
            message = envelope["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PlannerAPIError("planner returned an invalid response envelope") from exc

        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise PlannerAPIError("planner returned malformed tool arguments") from exc
            if not isinstance(arguments, dict):
                raise PlannerAPIError("planner tool arguments must be an object")
            calls.append(
                ToolCall(
                    id=str(item.get("id") or uuid.uuid4().hex),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )

        metadata = {"model": envelope.get("model", self.model), "usage": envelope.get("usage")}
        return PlannerResponse(message.get("content"), tuple(calls), metadata)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
