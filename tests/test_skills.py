import unittest

from android_agent.skills.loader import SkillRouter


class SkillRouterTests(unittest.TestCase):
    def test_core_is_always_loaded_and_files_are_progressive(self):
        router = SkillRouter.bundled()
        ordinary = router.instructions_for("what is my battery level")
        file_request = router.instructions_for("create a file and send it to me")

        self.assertIn("Skill: core-reliability", ordinary)
        self.assertNotIn("Skill: safe-files", ordinary)
        self.assertIn("Skill: safe-files", file_request)
        self.assertIn("Never call a tool", ordinary)

    def test_skill_catalog_is_trusted_and_complete(self):
        ids = {skill.skill_id for skill in SkillRouter.bundled().skills}
        self.assertEqual(
            ids,
            {
                "core-reliability",
                "device-control",
                "safe-files",
                "communications",
                "sensitive-data",
            },
        )


if __name__ == "__main__":
    unittest.main()
