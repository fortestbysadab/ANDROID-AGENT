import unittest

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
