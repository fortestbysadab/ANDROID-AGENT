"""OpenAI-compatible chat-completions planner adapter.

Works against OpenAI, Gemini's compatibility endpoint, and local servers such
as llama.cpp or vLLM. Provider schema quirks are handled in `compat.py` so the
agent's internal validation schema stays strict.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .base import PlannerResponse, ToolCall
from .compat import Dialect, adapt_messages, adapt_tools, normalize_base_url

logger = logging.getLogger(__name__)

#: Transient HTTP statuses worth retrying with backoff.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

#: Cap on the response body we are willing to buffer.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class PlannerError(RuntimeError):
    """Base class for planner failures."""

    #: Short, owner-facing hint describing how to fix the problem.
    remedy: str = ""


class PlannerAuthError(PlannerError):
    remedy = "Check ANDROID_AGENT_LLM_API_KEY is a valid key for this endpoint."


class PlannerModelError(PlannerError):
    remedy = (
        "Check ANDROID_AGENT_LLM_MODEL names a real model and "
        "ANDROID_AGENT_LLM_BASE_URL points at the right path."
    )


class PlannerSchemaError(PlannerError):
    remedy = "The provider rejected a tool schema; this is an agent bug, not a config issue."


class PlannerRateLimitError(PlannerError):
    remedy = "The provider is rate limiting. Wait, or switch to a higher quota tier."


class PlannerTransportError(PlannerError):
    remedy = "Check network connectivity and that the endpoint host is reachable."


class PlannerProtocolError(PlannerError):
    remedy = "The endpoint did not return an OpenAI-compatible response envelope."


# Backwards-compatible alias for the previous public name.
PlannerAPIError = PlannerError


def _classify(status: int, detail: str) -> PlannerError:
    """Map an HTTP failure onto a specific, actionable exception."""
    lowered = detail.lower()
    if status in (401, 403):
        return PlannerAuthError(f"HTTP {status}: authentication rejected. {detail[:400]}")
    if status == 429:
        return PlannerRateLimitError(f"HTTP {status}: rate limited. {detail[:400]}")
    if status == 404:
        return PlannerModelError(f"HTTP {status}: model or endpoint path not found. {detail[:400]}")
    if status == 400 and (
        "additionalproperties" in lowered
        or "function_declarations" in lowered
        or "unknown name" in lowered
        or "should be non-empty" in lowered
    ):
        return PlannerSchemaError(f"HTTP {status}: tool schema rejected. {detail[:600]}")
    if status == 400:
        return PlannerModelError(f"HTTP {status}: bad request. {detail[:600]}")
    return PlannerTransportError(f"HTTP {status}: {detail[:400]}")


class OpenAICompatiblePlanner:
    """Planner backed by a `POST {base_url}/chat/completions` endpoint.

    Credentials stay in HTTP headers and are never placed in model context,
    audit records, or log output.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.0,
        dialect: Dialect | str | None = None,
        max_retries: int = 3,
    ) -> None:
        if isinstance(dialect, str):
            dialect = Dialect(dialect)
        self.dialect = dialect or Dialect.detect(base_url)
        self.base_url = normalize_base_url(base_url, self.dialect)
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_retries = max(1, max_retries)
        if self.base_url != base_url.rstrip("/"):
            logger.warning(
                "Corrected LLM base URL for %s dialect: %r -> %r",
                self.dialect.value,
                base_url,
                self.base_url,
            )
        logger.info(
            "Planner ready: dialect=%s model=%s endpoint=%s/chat/completions",
            self.dialect.value,
            self.model,
            self.base_url,
        )

    # -- public API ---------------------------------------------------

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> PlannerResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": adapt_messages(messages, self.dialect),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = adapt_tools(tools, self.dialect)
            payload["tool_choice"] = "auto"

        envelope = self._post(payload)
        return self._parse(envelope)

    def health_check(self) -> dict[str, Any]:
        """Send a minimal request to prove credentials and model are usable."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "temperature": 0.0,
        }
        envelope = self._post(payload)
        message = envelope.get("choices", [{}])[0].get("message", {})
        return {"model": envelope.get("model", self.model), "content": message.get("content")}

    # -- internals ----------------------------------------------------

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        last_error: PlannerError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._request_once(payload)
            except PlannerError as exc:
                last_error = exc
                retryable = isinstance(exc, (PlannerRateLimitError, PlannerTransportError))
                if not retryable or attempt == self.max_retries:
                    raise
                delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
                logger.warning(
                    "Planner attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    self.max_retries,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
                continue

            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PlannerProtocolError("endpoint returned non-JSON content") from exc
            if not isinstance(envelope, dict):
                raise PlannerProtocolError("endpoint returned a non-object response")
            if "error" in envelope and "choices" not in envelope:
                raise PlannerModelError(f"provider error: {str(envelope['error'])[:500]}")
            return envelope

        raise last_error or PlannerTransportError("planner exhausted all retries")

    def _request_once(self, payload: Mapping[str, Any]) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            logger.error("Planner HTTP %s from %s: %s", exc.code, self.base_url, detail[:800])
            raise _classify(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlannerTransportError(f"request failed: {exc}") from exc

    def _parse(self, envelope: Mapping[str, Any]) -> PlannerResponse:
        try:
            message = envelope["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlannerProtocolError("response envelope has no choices[0].message") from exc
        if not isinstance(message, Mapping):
            raise PlannerProtocolError("choices[0].message is not an object")

        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function") or {}
            name = str(function.get("name") or "").strip()
            if not name:
                logger.warning("Discarding tool call with no name")
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, Mapping):
                arguments: Any = dict(raw_arguments)
            else:
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise PlannerProtocolError(
                        f"tool {name} returned malformed JSON arguments"
                    ) from exc
            if not isinstance(arguments, dict):
                raise PlannerProtocolError(f"tool {name} arguments must be a JSON object")
            calls.append(
                ToolCall(
                    id=str(item.get("id") or uuid.uuid4().hex),
                    name=name,
                    arguments=arguments,
                )
            )

        content = message.get("content")
        if isinstance(content, list):
            # Some providers return content parts instead of a plain string.
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, Mapping) and part.get("type") in (None, "text")
            )

        metadata = {
            "model": envelope.get("model", self.model),
            "usage": envelope.get("usage"),
            "finish_reason": (envelope.get("choices") or [{}])[0].get("finish_reason"),
        }
        return PlannerResponse(content, tuple(calls), metadata)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "android-agent/2",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
