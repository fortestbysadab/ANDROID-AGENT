"""Provider quirk normalization for the OpenAI-compatible wire format.

The agent's internal JSON Schema is intentionally strict: closed objects,
`additionalProperties: false`, length bounds, patterns. That strictness is a
security boundary and must never be relaxed for local validation.

Some providers, however, do not accept the full JSON Schema vocabulary on the
wire. Google's Gemini `function_declarations` accepts only a restricted
OpenAPI 3.0 subset and returns HTTP 400 for unknown keywords such as
`additionalProperties`. This module downgrades the *outbound* schema only.
Inbound arguments are still validated against the original strict schema.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any
from urllib.parse import urlparse

#: Keywords Gemini's function-declaration parser rejects outright.
GEMINI_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "patternProperties",
        "propertyNames",
        "const",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "allOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "examples",
        "default",
        "multipleOf",
        "uniqueItems",
        "readOnly",
        "writeOnly",
    }
)

#: Keywords Gemini does understand, so they are preserved.
GEMINI_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "anyOf",
    }
)


class Dialect(str, Enum):
    """Wire dialect for tool schemas."""

    OPENAI = "openai"
    GEMINI = "gemini"

    @classmethod
    def detect(cls, base_url: str) -> Dialect:
        """Infer the dialect from the endpoint host.

        Explicit configuration always wins; this is only the default.
        """
        host = (urlparse(base_url).hostname or "").lower()
        if host.endswith("generativelanguage.googleapis.com"):
            return cls.GEMINI
        if host.endswith("googleapis.com") and "aiplatform" in host:
            return cls.GEMINI
        return cls.OPENAI


def _strip_for_gemini(node: Any) -> Any:
    """Recursively remove keywords Gemini's schema parser rejects."""
    if isinstance(node, Mapping):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key in GEMINI_UNSUPPORTED_KEYWORDS:
                continue
            cleaned[key] = _strip_for_gemini(value)
        return cleaned
    if isinstance(node, (list, tuple)):
        return [_strip_for_gemini(item) for item in node]
    return node


def _has_properties(schema: Mapping[str, Any]) -> bool:
    return bool(schema.get("properties"))


def adapt_tools(
    tools: Sequence[Mapping[str, Any]], dialect: Dialect
) -> list[dict[str, Any]]:
    """Return wire-safe copies of the tool declarations.

    For `Dialect.OPENAI` the schemas pass through unchanged. For
    `Dialect.GEMINI` unsupported keywords are stripped and parameterless tools
    omit `parameters` entirely, because Gemini rejects an OBJECT schema whose
    `properties` map is empty.
    """
    if dialect is Dialect.OPENAI:
        return [copy.deepcopy(dict(tool)) for tool in tools]

    adapted: list[dict[str, Any]] = []
    for tool in tools:
        entry = copy.deepcopy(dict(tool))
        function = dict(entry.get("function") or {})
        parameters = function.get("parameters")
        if isinstance(parameters, Mapping):
            if _has_properties(parameters):
                function["parameters"] = _strip_for_gemini(dict(parameters))
            else:
                # Gemini errors on `{"type":"object","properties":{}}`.
                function.pop("parameters", None)
        entry["function"] = function
        adapted.append(entry)
    return adapted


#: Documented escape hatch. Gemini accepts this sentinel in place of a real
#: signature for history it never signed (for example a tool call replayed
#: after a restart, or one produced by a different model). Real signatures are
#: always preferred; this only prevents a hard 400 on unsignable history.
UNSIGNED_PLACEHOLDER = "skip_thought_signature_validator"


def _gemini_tool_calls(tool_calls: Any, first_is_signed: bool) -> Any:
    """Ensure the leading function call carries a thought signature.

    Gemini 3 thinking models reject a replayed turn whose first `functionCall`
    part has no `thought_signature`. Parallel calls only ever carry a
    signature on the first call, so only that one needs repair.
    """
    if not isinstance(tool_calls, (list, tuple)) or first_is_signed:
        return tool_calls
    repaired = [dict(call) if isinstance(call, Mapping) else call for call in tool_calls]
    head = repaired[0]
    if isinstance(head, dict):
        head["extra_content"] = {"google": {"thought_signature": UNSIGNED_PLACEHOLDER}}
    return repaired


def _first_call_is_signed(tool_calls: Any) -> bool:
    if not isinstance(tool_calls, (list, tuple)) or not tool_calls:
        return True
    head = tool_calls[0]
    if not isinstance(head, Mapping):
        return True
    signature = (
        (head.get("extra_content") or {}).get("google", {}).get("thought_signature")
        if isinstance(head.get("extra_content"), Mapping)
        else None
    )
    return bool(signature)


def adapt_messages(
    messages: Sequence[Mapping[str, Any]], dialect: Dialect
) -> list[dict[str, Any]]:
    """Normalize conversation messages for the target dialect.

    Gemini's compatibility layer rejects a null `content` on an assistant
    message that carries `tool_calls`, ignores the non-standard `name` field on
    tool results, and requires each replayed function call to carry back the
    `thought_signature` it issued.

    Providers other than Gemini have no use for `extra_content`, so it is
    removed to avoid sending an unknown field to a strict endpoint.
    """
    normalized: list[dict[str, Any]] = []
    for message in messages:
        entry = dict(message)
        role = entry.get("role")

        if dialect is Dialect.GEMINI:
            if role == "assistant":
                if entry.get("tool_calls"):
                    entry.pop("content", None)
                    entry["tool_calls"] = _gemini_tool_calls(
                        entry["tool_calls"], _first_call_is_signed(entry["tool_calls"])
                    )
                elif entry.get("content") is None:
                    entry["content"] = ""
            if role == "tool":
                entry.pop("name", None)
        else:
            if role == "assistant" and isinstance(entry.get("tool_calls"), (list, tuple)):
                entry["tool_calls"] = [
                    {k: v for k, v in call.items() if k != "extra_content"}
                    if isinstance(call, Mapping)
                    else call
                    for call in entry["tool_calls"]
                ]
        normalized.append(entry)
    return normalized


def normalize_base_url(base_url: str, dialect: Dialect) -> str:
    """Repair the most common Gemini base-URL mistake.

    The compatibility endpoint lives under `/v1beta/openai`, not `/v1beta`
    and not `/v1`. A wrong path yields an opaque HTTP 404.
    """
    trimmed = base_url.rstrip("/")
    if dialect is not Dialect.GEMINI:
        return trimmed
    if trimmed.endswith("/openai"):
        return trimmed
    if trimmed.endswith("/v1beta") or trimmed.endswith("/v1"):
        return trimmed.rsplit("/", 1)[0] + "/v1beta/openai"
    if "generativelanguage.googleapis.com" in trimmed:
        return trimmed + "/v1beta/openai"
    return trimmed
