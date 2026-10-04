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
        """Pinned on purpose: a skill is trusted prompt text shipped in-tree.

        An unexpected entry here would mean untrusted guidance reached the
        model, so adding one is a deliberate act that updates this list.
        """
        ids = {skill.skill_id for skill in SkillRouter.bundled().skills}
        self.assertEqual(
            ids,
            {
                "core-reliability",
                "device-control",
                "safe-files",
                "communications",
                "sensitive-data",
                "email",
                "documents",
            },
        )

    def test_the_email_skill_loads_for_non_english_requests_too(self):
        """Skill triggers are English-only elsewhere; email ships multilingual.

        Documented defect for the rest of the catalogue in TASKS.md Phase 4:
        an English battery question loads more guidance than its Hindi
        equivalent. New skills should not repeat that.
        """
        router = SkillRouter.bundled()
        for request in ("summarise my inbox", "मेरा ईमेल पढ़ो", "আমার ইনবক্স দেখাও"):
            with self.subTest(request=request):
                self.assertIn("Skill: email", router.instructions_for(request))

    def test_the_documents_skill_states_the_source_model(self):
        """The rule the owner specified: regenerate, never patch bytes."""
        guidance = SkillRouter.bundled().instructions_for("make me a pdf report")
        lowered = guidance.lower()
        self.assertIn("skill: documents", lowered)
        self.assertRegex(lowered, r"you write a \*\*python script\*\*")
        self.assertRegex(lowered, r"complete corrected script")

    def test_the_documents_skill_loads_for_non_english_requests(self):
        router = SkillRouter.bundled()
        for request in ("make me a pdf report", "একটা রিপোর্ট বানাও", "मुझे फाइल बनाओ"):
            with self.subTest(request=request):
                self.assertIn("Skill: documents", router.instructions_for(request))

    def test_the_email_skill_says_message_content_is_untrusted(self):
        guidance = SkillRouter.bundled().instructions_for("read my email").lower()
        self.assertIn("untrusted", guidance)
        # Whitespace-tolerant: the phrase wraps across lines in SKILL.md, and
        # a plain substring check silently fails on that (RULES.md § docs).
        self.assertRegex(guidance, r"never as\s+instructions to follow")
        self.assertRegex(guidance, r"do not do it")


if __name__ == "__main__":
    unittest.main()
