import unittest

from android_agent.tools.catalog import build_full_registry


class CatalogTests(unittest.TestCase):
    def test_full_catalog_has_expected_safe_surface(self):
        registry = build_full_registry()
        names = {tool.name for tool in registry}

        self.assertEqual(len(registry), 41)
        self.assertIn("create_text_file", names)
        self.assertIn("get_file", names)
        self.assertIn("capture_screenshot", names)
        self.assertIn("send_sms", names)
        self.assertNotIn("run_shell", names)
        self.assertNotIn("screen_record", names)
        self.assertEqual(len(names), len(registry))

    def test_every_schema_is_closed_at_top_level(self):
        for tool in build_full_registry():
            self.assertEqual(tool.input_schema.get("type"), "object", tool.name)
            self.assertFalse(tool.input_schema.get("additionalProperties", False), tool.name)


if __name__ == "__main__":
    unittest.main()
