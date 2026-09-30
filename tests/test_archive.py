"""Tests for the web console's conversation archive."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from android_agent.web.archive import (
    TITLE_LIMIT,
    ChatArchive,
    derive_title,
    new_conversation_id,
)


def transcript(*texts, role="user"):
    return [{"role": role, "text": text, "at": 1.0} for text in texts]


class TitleTests(unittest.TestCase):
    def test_title_comes_from_the_first_user_message(self):
        entries = [
            {"role": "assistant", "text": "Hello"},
            {"role": "user", "text": "turn the torch on"},
            {"role": "user", "text": "and off"},
        ]
        self.assertEqual(derive_title(entries), "turn the torch on")

    def test_long_titles_are_truncated_with_an_ellipsis(self):
        title = derive_title(transcript("x" * 200))
        self.assertLessEqual(len(title), TITLE_LIMIT)
        self.assertTrue(title.endswith("…"))

    def test_whitespace_is_collapsed(self):
        self.assertEqual(derive_title(transcript("a\n\n  b   c")), "a b c")

    def test_transcript_without_a_user_message_gets_a_placeholder(self):
        self.assertEqual(derive_title(transcript("hi", role="assistant")), "New chat")

    def test_ids_are_unique(self):
        self.assertEqual(len({new_conversation_id() for _ in range(200)}), 200)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chats.db"
        self.archive = ChatArchive(self.path, max_conversations=3)
        self.addCleanup(self.archive.close)

    def test_saved_conversation_round_trips(self):
        self.archive.save("a", transcript("hello"))
        self.assertEqual(self.archive.transcript("a"), transcript("hello"))

    def test_empty_transcripts_are_not_stored(self):
        self.archive.save("a", [])
        self.assertEqual(self.archive.recent(), [])
        self.assertIsNone(self.archive.transcript("a"))

    def test_saving_again_updates_in_place(self):
        self.archive.save("a", transcript("one"))
        self.archive.save("a", transcript("one", "two"))
        summaries = self.archive.recent()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].message_count, 2)

    def test_recent_is_ordered_by_last_update(self):
        self.archive.save("a", transcript("first"), now=100)
        self.archive.save("b", transcript("second"), now=200)
        self.archive.save("a", transcript("first", "again"), now=300)
        self.assertEqual([s.conversation_id for s in self.archive.recent()], ["a", "b"])

    def test_oldest_conversations_fall_off_past_the_cap(self):
        for index in range(5):
            self.archive.save(f"c{index}", transcript(f"chat {index}"), now=100 + index)
        kept = [s.conversation_id for s in self.archive.recent()]
        self.assertEqual(kept, ["c4", "c3", "c2"])
        self.assertIsNone(self.archive.transcript("c0"))

    def test_updating_an_old_chat_rescues_it_from_pruning(self):
        for index in range(3):
            self.archive.save(f"c{index}", transcript("x"), now=100 + index)
        self.archive.save("c0", transcript("x", "revived"), now=400)
        self.archive.save("new", transcript("y"), now=500)
        kept = [s.conversation_id for s in self.archive.recent()]
        self.assertIn("c0", kept)
        self.assertNotIn("c1", kept)

    def test_delete_removes_one_conversation(self):
        self.archive.save("a", transcript("one"))
        self.archive.save("b", transcript("two"))
        self.assertTrue(self.archive.delete("a"))
        self.assertFalse(self.archive.delete("a"))
        self.assertEqual([s.conversation_id for s in self.archive.recent()], ["b"])

    def test_clear_removes_everything(self):
        self.archive.save("a", transcript("one"))
        self.archive.save("b", transcript("two"))
        self.assertEqual(self.archive.clear(), 2)
        self.assertEqual(self.archive.recent(), [])

    def test_missing_conversation_reads_as_none(self):
        self.assertIsNone(self.archive.transcript("nope"))

    def test_data_survives_a_restart(self):
        self.archive.save("a", transcript("remember me"))
        self.archive.close()
        reopened = ChatArchive(self.path, max_conversations=3)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.transcript("a"), transcript("remember me"))

    def test_corrupt_payload_is_dropped_rather_than_raising(self):
        self.archive.save("a", transcript("one"))
        self.archive._connection.execute(
            "UPDATE conversations SET transcript = ? WHERE conversation_id = ?",
            ("{not json", "a"),
        )
        self.archive._connection.commit()
        self.assertIsNone(self.archive.transcript("a"))
        self.assertEqual(self.archive.recent(), [])

    def test_recent_limit_is_capped_by_the_configured_maximum(self):
        for index in range(3):
            self.archive.save(f"c{index}", transcript("x"), now=100 + index)
        self.assertEqual(len(self.archive.recent(limit=99)), 3)
        self.assertEqual(len(self.archive.recent(limit=1)), 1)

    def test_invalid_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            ChatArchive(Path(self.tmp.name) / "bad.db", max_conversations=0)


if __name__ == "__main__":
    unittest.main()
