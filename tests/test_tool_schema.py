import unittest
from typing import ClassVar

from android_agent.tools.base import Risk, SchemaValidationError, ToolResult, ToolSpec


class ToolSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tool = ToolSpec(
            name="example_tool",
            description="A test tool with a deliberately strict input shape.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 10},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "mode": {"type": "string", "enum": ["a", "b"]},
                },
                "required": ["name", "count"],
                "additionalProperties": False,
            },
            risk=Risk.READ_ONLY,
            handler=lambda context, args: ToolResult.ok("ok"),
        )

    def test_accepts_valid_input(self):
        self.assertEqual(
            self.tool.validate({"name": "phone", "count": 2, "mode": "a"}),
            {"name": "phone", "count": 2, "mode": "a"},
        )

    def test_rejects_unknown_fields(self):
        with self.assertRaisesRegex(SchemaValidationError, "unknown fields"):
            self.tool.validate({"name": "phone", "count": 2, "secret": "x"})

    def test_rejects_bool_as_integer(self):
        with self.assertRaisesRegex(SchemaValidationError, "must be an integer"):
            self.tool.validate({"name": "phone", "count": True})

    def test_rejects_invalid_tool_name_for_provider_portability(self):
        with self.assertRaisesRegex(ValueError, "invalid tool name"):
            ToolSpec(
                name="device.shell",
                description="Not valid.",
                input_schema={"type": "object", "properties": {}},
                risk=Risk.CRITICAL,
                handler=lambda context, args: ToolResult.ok("no"),
            )


if __name__ == "__main__":
    unittest.main()


class AdditionalPropertiesTests(unittest.TestCase):
    """A schema that allows extra fields must also preserve them.

    Dropping them silently is the dangerous version: the caller gets a
    success and an empty object, and the omission only shows up later as a
    tool running with no arguments.
    """

    FREEFORM: ClassVar[dict] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    NESTED: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "payload": {"type": "object", "additionalProperties": True},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def _spec(self, schema):
        return ToolSpec(
            "probe", "Probe.", schema, Risk.READ_ONLY,
            lambda context, arguments: ToolResult.ok("ok", {}),
        )

    def test_undeclared_fields_are_preserved_when_allowed(self):
        validated = self._spec(self.NESTED).validate(
            {"name": "x", "payload": {"number": "+91900", "text": "hi"}}
        )
        self.assertEqual(validated["payload"], {"number": "+91900", "text": "hi"})

    def test_an_empty_free_form_object_stays_empty(self):
        validated = self._spec(self.NESTED).validate({"name": "x", "payload": {}})
        self.assertEqual(validated["payload"], {})

    def test_nested_values_of_any_json_type_survive(self):
        payload = {"n": 3, "flag": True, "list": [1, 2], "inner": {"a": "b"}}
        validated = self._spec(self.NESTED).validate({"name": "x", "payload": payload})
        self.assertEqual(validated["payload"], payload)

    def test_extra_fields_are_still_rejected_where_not_allowed(self):
        with self.assertRaises(SchemaValidationError):
            self._spec(self.FREEFORM).validate({"name": "x", "surprise": 1})

    def test_declared_properties_are_still_validated(self):
        with self.assertRaises(SchemaValidationError):
            self._spec(self.NESTED).validate({"name": 5, "payload": {}})
