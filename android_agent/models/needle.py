"""Optional on-device fast path backed by Cactus Compute's Needle.

Needle is a tiny (8-35 MB) tool-calling specialist that runs entirely on the
device. It is deliberately **not** a chat model: it maps a request onto a
declared tool and fills the arguments, returning a calibrated confidence
score. See https://github.com/cactus-compute/needle.

Why this is a router and not a planner replacement
--------------------------------------------------
Needle cannot hold a conversation, summarize, or reason about anything
outside its declared tools. It is used here exactly as the architecture
blueprint specifies:

* `complete()` only. Never `Needle.run()`, which would execute Python
  callables itself and bypass this project's validator, policy engine,
  approval flow, and audit trail.
* Confidence is **routing evidence, never authorization**. Every proposal
  Needle makes still passes through schema validation and the deterministic
  policy engine exactly like a cloud proposal.
* Only an explicit allowlist of simple, low-risk tools is eligible. Anything
  sensitive, external, or destructive goes to the cloud planner regardless of
  how confident Needle is.

The payoff: common device commands ("turn on the torch", "battery?") are
answered offline in milliseconds with no API call, no cost, and no data
leaving the phone. Anything else transparently escalates.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .base import Planner, PlannerResponse, ToolCall

logger = logging.getLogger(__name__)

#: Tools the on-device model may select without any cloud involvement.
#: Deliberately small: read-only status and trivially reversible actuators.
#: Nothing here reads personal data or causes an external side effect.
DEFAULT_FAST_PATH_TOOLS = frozenset(
    {
        "get_battery_status",
        "get_system_info",
        "get_volume_levels",
        "get_current_app",
        "set_torch",
        "set_brightness",
        "set_volume",
        "vibrate_device",
        "control_media",
        "show_toast",
    }
)

#: Below this, escalate to the cloud planner. Cactus documents the contract as
#: "act at or above a threshold, re-ask or route to a bigger model below it".
DEFAULT_CONFIDENCE_THRESHOLD = 0.85


class NeedleAgent(Protocol):
    """The slice of the Needle API this module depends on."""

    def complete(self, text: str) -> Mapping[str, Any]:
        ...


class NeedleUnavailable(RuntimeError):
    """Raised when the Needle package or engine cannot be loaded."""


def load_needle_agent(
    tools: Sequence[Mapping[str, Any]], model: str | None = None
) -> NeedleAgent:
    """Construct a Needle agent from OpenAI-style tool declarations.

    Raises `NeedleUnavailable` with an actionable message when the package or
    its prebuilt engine is missing, which is common on Termux: the published
    wheels target glibc/musl Linux, and Termux is bionic.
    """
    try:
        import needle  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NeedleUnavailable(
            "cactus-needle is not installed. Try `pip install cactus-needle`. "
            "On Termux the prebuilt engine may not load; see "
            "https://cactuscompute.com/blog/needle-supported-devices"
        ) from exc

    declarations = [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "parameters": tool["function"].get("parameters", {"type": "object", "properties": {}}),
        }
        for tool in tools
    ]
    try:
        agent = needle.Agent(tools=declarations, model=model) if model else needle.Agent(
            tools=declarations
        )
    except Exception as exc:  # engine download/load failures are varied
        raise NeedleUnavailable(f"Needle engine failed to load: {exc}") from exc
    logger.info("Needle fast path ready with %d tools", len(declarations))
    return agent


class NeedleRouter:
    """Wraps a cloud planner with an on-device fast path.

    Implements the `Planner` protocol, so the runtime needs no changes and
    every proposal still flows through validation, policy, and audit.
    """

    def __init__(
        self,
        *,
        cloud: Planner,
        agent: NeedleAgent | None,
        allowed_tools: frozenset[str] = DEFAULT_FAST_PATH_TOOLS,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        answer_locally: bool = True,
    ) -> None:
        self.cloud = cloud
        self.agent = agent
        self.allowed_tools = allowed_tools
        self.confidence_threshold = confidence_threshold
        self.answer_locally = answer_locally
        #: Call ids this router produced, so it knows which tool results it
        #: is allowed to phrase an answer for.
        self._local_call_ids: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self.agent is not None

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> PlannerResponse:
        if self.answer_locally:
            local = self._answer_from_local_results(messages)
            if local is not None:
                return local

        proposal = self._try_fast_path(messages, tools)
        if proposal is not None:
            return proposal
        return self.cloud.complete(messages, tools)

    # -- fast path ----------------------------------------------------

    def _try_fast_path(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> PlannerResponse | None:
        if self.agent is None:
            return None
        # Only route the opening turn. Once tool results exist the task is
        # conversational, which Needle is explicitly not built for.
        if any(message.get("role") == "tool" for message in messages):
            return None
        user_text = _last_user_text(messages)
        if not user_text:
            return None

        declared = {tool["function"]["name"] for tool in tools}

        try:
            response = self.agent.complete(user_text)
        except Exception:
            logger.warning("Needle fast path failed; escalating to cloud", exc_info=True)
            return None

        if not isinstance(response, Mapping) or response.get("type") != "call":
            logger.debug("Needle returned no call; escalating")
            return None

        confidence = response.get("confidence")
        # Fine-tuned Needle builds report confidence as None. Without a
        # calibrated score there is no basis to skip the cloud planner.
        if not isinstance(confidence, (int, float)):
            logger.info("Needle gave no confidence score; escalating")
            return None
        if confidence < self.confidence_threshold:
            logger.info(
                "Needle confidence %.2f below threshold %.2f; escalating",
                confidence,
                self.confidence_threshold,
            )
            return None

        calls = response.get("function_calls") or []
        if not calls or len(calls) > 1:
            # Multi-step requests need a real planner to sequence them.
            return None

        name = str(calls[0].get("name", ""))
        if name not in self.allowed_tools:
            logger.info("Needle picked %r, which is not fast-path eligible; escalating", name)
            return None
        if name not in declared:
            logger.warning("Needle picked undeclared tool %r; escalating", name)
            return None

        arguments = calls[0].get("arguments")
        if not isinstance(arguments, Mapping):
            return None

        call_id = f"needle-{abs(hash((name, user_text))) % 10**10}"
        self._local_call_ids.add(call_id)
        logger.info("Needle fast path selected %s (confidence %.2f)", name, confidence)
        return PlannerResponse(
            None,
            (ToolCall(call_id, name, dict(arguments)),),
            {"router": "needle", "confidence": float(confidence)},
        )

    def _answer_from_local_results(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> PlannerResponse | None:
        """Phrase a reply from tool results this router itself produced.

        Needle cannot summarize, so rather than pay for a cloud turn just to
        restate a tool result, the deterministic summary is returned directly.
        This keeps simple device commands fully offline.
        """
        if not self._local_call_ids:
            return None
        results = [
            message
            for message in messages
            if message.get("role") == "tool"
            and str(message.get("tool_call_id")) in self._local_call_ids
        ]
        if not results:
            return None
        # Any tool result the router did not originate means the cloud planner
        # is mid-conversation; do not interfere.
        all_tool_messages = [m for m in messages if m.get("role") == "tool"]
        if len(all_tool_messages) != len(results):
            return None

        import json

        summaries: list[str] = []
        for message in results:
            self._local_call_ids.discard(str(message.get("tool_call_id")))
            try:
                payload = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                return None
            if payload.get("status") != "ok":
                # Errors deserve a real explanation; escalate.
                return None
            summaries.append(str(payload.get("summary", "")).strip())

        text = " ".join(part for part in summaries if part)
        if not text:
            return None
        return PlannerResponse(text, (), {"router": "needle", "local_answer": True})


def _last_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""
