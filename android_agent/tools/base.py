"""Typed tool definitions and schema validation."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Risk(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    SENSITIVE_READ = "sensitive_read"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DEVICE_MUTATION = "device_mutation"
    RAW_CONTROL = "raw_control"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ToolContext:
    actor_id: str
    chat_id: int
    run_id: str
    direct_user_request: bool = True
    #: True once this run has read content written by someone other than the
    #: owner - an email body, an SMS, a notification. From that point the
    #: model's proposals may be echoing an instruction planted by a stranger,
    #: so the policy stops treating "the owner asked for this" as given.
    tainted: bool = False


@dataclass(frozen=True)
class ToolResult:
    status: str
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False
    error_code: str | None = None

    @classmethod
    def ok(cls, summary: str, data: Mapping[str, Any] | None = None) -> ToolResult:
        return cls("ok", summary, data or {})

    @classmethod
    def error(
        cls, summary: str, *, code: str = "tool_error", retryable: bool = False
    ) -> ToolResult:
        return cls("error", summary, {}, retryable, code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "data": dict(self.data),
            "retryable": self.retryable,
            "error_code": self.error_code,
        }


ToolHandler = Callable[[ToolContext, Mapping[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    risk: Risk
    handler: ToolHandler
    timeout_seconds: float = 15.0
    version: str = "1"
    idempotent: bool = False
    #: Set on tools whose output is written by third parties. Reading one
    #: taints the rest of the run. This is a property of the data source, not
    #: of the risk level: get_battery_status is sensitive to nobody, while
    #: get_recent_sms hands the model text a stranger composed.
    returns_untrusted_content: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ValueError(f"tool {self.name} needs a description")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise SchemaValidationError("arguments must be an object")
        value = _validate_value(dict(arguments), self.input_schema, "arguments")
        if not isinstance(value, dict):
            raise SchemaValidationError("arguments must validate to an object")
        return value

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


class SchemaValidationError(ValueError):
    pass


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{path} must be an object")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        missing = sorted(required - set(value))
        if missing:
            raise SchemaValidationError(f"{path} is missing: {', '.join(missing)}")
        allows_extra = schema.get("additionalProperties", False) is True
        if not allows_extra:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{path} has unknown fields: {', '.join(unknown)}")
        validated = {
            key: _validate_value(item, properties[key], f"{path}.{key}")
            for key, item in value.items()
            if key in properties
        }
        if allows_extra:
            # Pass undeclared fields through rather than dropping them. A
            # schema that permits extra properties and then silently discards
            # them is worse than one that rejects them: the caller sees a
            # success and an empty object. Used by schedule_task, whose
            # tool_arguments are opaque here and revalidated against the
            # target tool's own schema before the task ever runs.
            validated.update(
                {key: item for key, item in value.items() if key not in properties}
            )
        return validated
    if expected == "array":
        if not isinstance(value, list):
            raise SchemaValidationError(f"{path} must be an array")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(f"{path} has too many items")
        item_schema = schema.get("items", {})
        return [_validate_value(item, item_schema, f"{path}[{i}]") for i, item in enumerate(value)]
    if expected == "string":
        if not isinstance(value, str):
            raise SchemaValidationError(f"{path} must be a string")
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(f"{path} is too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise SchemaValidationError(f"{path} has an invalid format")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaValidationError(f"{path} must be an integer")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaValidationError(f"{path} must be a number")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise SchemaValidationError(f"{path} must be a boolean")
    elif expected is not None:
        raise SchemaValidationError(f"{path} uses unsupported schema type {expected!r}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path} must be one of {schema['enum']!r}")
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaValidationError(f"{path} is below minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaValidationError(f"{path} is above maximum {schema['maximum']}")
    return value
