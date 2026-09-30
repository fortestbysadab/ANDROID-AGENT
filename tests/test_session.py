"""Session expiry, persistence, and history-structure invariants."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from android_agent.agent.session import (
    SqliteSessionStore,
    close_open_tool_calls,
    trim_history,
)

TTL = 900.0


def _user(text):
    return {"role": "user", "content": text}


def _assistant_call(call_id, name="get_battery_status"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": "{}"}}
        ],
    }


def _tool(call_id, name="get_battery_status"):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": "{}"}


class OrphanTests(unittest.TestCase):
    """An unanswered tool call makes the provider reject the next request."""

    def test_pending_approval_turn_is_closed(self):
        messages = [_user("send an sms"), _assistant_call("c1", "send_sms")]
        closed = close_open_tool_calls(messages)
        self.assertEqual(closed[-1]["role"], "tool")
        self.assertEqual(closed[-1]["tool_call_id"], "c1")
        self.assertEqual(json.loads(closed[-1]["content"])["status"], "not_executed")

    def test_answered_calls_are_untouched(self):
        messages = [_user("battery"), _assistant_call("c1"), _tool("c1")]
        self.assertEqual(close_open_tool_calls(messages), [dict(m) for m in messages])

    def test_parallel_calls_all_closed(self):
        assistant = {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "x", "arguments": "{}"}},
                {"id": "b", "function": {"name": "y", "arguments": "{}"}},
            ],
        }
        closed = close_open_tool_calls([_user("do both"), assistant])
        ids = [m["tool_call_id"] for m in closed if m["role"] == "tool"]
        self.assertEqual(sorted(ids), ["a", "b"])


class TrimTests(unittest.TestCase):
    def test_trimming_never_orphans_a_tool_call(self):
        messages = []
        for index in range(20):
            messages += [_user(f"q{index}"), _assistant_call(f"c{index}"), _tool(f"c{index}")]

        trimmed = trim_history(messages, max_messages=10)
        answered = {m["tool_call_id"] for m in trimmed if m["role"] == "tool"}
        for message in trimmed:
            for call in message.get("tool_calls") or []:
                self.assertIn(call["id"], answered, "trim orphaned a tool call")

    def test_trim_cuts_at_user_boundaries(self):
        messages = []
        for index in range(10):
            messages += [_user(f"q{index}"), {"role": "assistant", "content": f"a{index}"}]
        trimmed = trim_history(messages, max_messages=6)
        self.assertEqual(trimmed[0]["role"], "user")

    def test_system_messages_are_never_stored(self):
        messages = [{"role": "system", "content": "prompt"}, _user("hi")]
        self.assertEqual([m["role"] for m in trim_history(messages)], ["user"])

    def test_most_recent_turn_survives_a_tiny_budget(self):
        messages = [_user("old"), _user("newest")]
        trimmed = trim_history(messages, max_messages=1)
        self.assertEqual(trimmed[-1]["content"], "newest")

    def test_byte_budget_is_enforced(self):
        messages = [_user("x" * 5000) for _ in range(20)]
        trimmed = trim_history(messages, max_messages=100, max_bytes=20_000)
        self.assertLess(len(trimmed), 20)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SqliteSessionStore(
            Path(self.directory.name) / "s.db", ttl_seconds=TTL
        )
        self.addCleanup(self.store.close)

    def test_no_session_initially(self):
        self.assertIsNone(self.store.active(1))

    def test_save_then_read_round_trip(self):
        self.store.save(1, [_user("hello")], now=1000.0)
        session = self.store.active(1, now=1001.0)
        self.assertIsNotNone(session)
        self.assertEqual(session.messages[0]["content"], "hello")
        self.assertEqual(session.turns, 1)

    def test_session_expires_after_ttl(self):
        self.store.save(1, [_user("hello")], now=1000.0)
        self.assertIsNotNone(self.store.active(1, now=1000.0 + TTL - 1))
        self.assertIsNone(self.store.active(1, now=1000.0 + TTL + 1))

    def test_idle_timer_resets_on_activity(self):
        """Fifteen minutes of inactivity, not fifteen minutes total."""
        self.store.save(1, [_user("a")], now=1000.0)
        self.store.save(1, [_user("a"), _user("b")], now=1000.0 + TTL - 10)
        self.assertIsNotNone(self.store.active(1, now=1000.0 + TTL + 100))

    def test_expired_session_starts_a_new_id(self):
        first = self.store.save(1, [_user("a")], now=1000.0)
        second = self.store.save(1, [_user("b")], now=1000.0 + TTL + 1)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(second.turns, 1)

    def test_turn_counter_increments_within_a_session(self):
        self.store.save(1, [_user("a")], now=1000.0)
        session = self.store.save(1, [_user("a"), _user("b")], now=1010.0)
        self.assertEqual(session.turns, 2)

    def test_reset_clears_context(self):
        self.store.save(1, [_user("secret")], now=1000.0)
        self.assertTrue(self.store.reset(1))
        self.assertIsNone(self.store.active(1, now=1001.0))
        self.assertFalse(self.store.reset(1))

    def test_chats_are_isolated(self):
        self.store.save(1, [_user("one")], now=1000.0)
        self.store.save(2, [_user("two")], now=1000.0)
        self.assertEqual(self.store.active(1, now=1001.0).messages[0]["content"], "one")
        self.assertEqual(self.store.active(2, now=1001.0).messages[0]["content"], "two")

    def test_history_survives_restart(self):
        path = Path(self.directory.name) / "persist.db"
        first = SqliteSessionStore(path, ttl_seconds=TTL)
        first.save(7, [_user("remember me")], now=1000.0)
        first.close()

        second = SqliteSessionStore(path, ttl_seconds=TTL)
        session = second.active(7, now=1100.0)
        self.assertIsNotNone(session, "Android kills Termux; sessions must persist")
        self.assertEqual(session.messages[0]["content"], "remember me")
        second.close()

    def test_open_tool_call_is_closed_on_save(self):
        session = self.store.save(
            1, [_user("send sms"), _assistant_call("c1", "send_sms")], now=1000.0
        )
        self.assertEqual(session.messages[-1]["role"], "tool")

    def test_purge_expired(self):
        self.store.save(1, [_user("a")], now=1000.0)
        self.store.save(2, [_user("b")], now=1000.0)
        self.assertEqual(self.store.purge_expired(now=1000.0 + TTL + 1), 2)

    def test_expires_in_counts_down(self):
        self.store.save(1, [_user("a")], now=1000.0)
        session = self.store.active(1, now=1000.0 + 300)
        self.assertAlmostEqual(session.expires_in(TTL, now=1000.0 + 300), TTL - 300, places=0)


if __name__ == "__main__":
    unittest.main()
