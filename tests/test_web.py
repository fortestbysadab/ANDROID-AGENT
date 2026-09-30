"""Tests for the local web console.

These run a real ThreadingHTTPServer on an ephemeral port and talk to it over
real HTTP, because most of what matters here (cookies, custom headers, status
codes, path handling) lives in the HTTP layer and would be invisible to a test
that called the handler methods directly.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

from android_agent.agent.runtime import AgentRuntime
from android_agent.agent.session import SqliteSessionStore
from android_agent.approvals.store import InMemoryApprovalStore
from android_agent.models.base import PlannerResponse, ToolCall
from android_agent.observability.audit import MemoryAuditSink
from android_agent.policy.engine import DefaultPolicy
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.registry import ToolRegistry
from android_agent.web.archive import ChatArchive
from android_agent.web.security import AuthManager, WebConfigError, validate_token
from android_agent.web.server import COOKIE_NAME, CSRF_HEADER, UI_PATH, WebApp, make_server

OWNER = 4242
TOKEN = "s" * 24

SCHEMA = {
    "type": "object",
    "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 15}},
    "required": ["level"],
    "additionalProperties": False,
}


class ScriptedPlanner:
    """Returns queued responses; repeats the last one if the loop runs on."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(list(messages))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def make_tool(name="set_volume", risk=Risk.REVERSIBLE, calls=None):
    def handler(context, arguments):
        if calls is not None:
            calls.append(arguments)
        return ToolResult.ok(f"{name} done", {"level": arguments["level"]})

    return ToolSpec(
        name=name,
        description="Set the Android media volume to a bounded raw level.",
        input_schema=SCHEMA,
        risk=risk,
        handler=handler,
        timeout_seconds=1,
    )


class Client:
    """Tiny HTTP client that remembers the session cookie."""

    def __init__(self, base: str):
        self.base = base
        self.cookie: str | None = None
        self.last_set_cookie: str | None = None

    def request(self, method, path, body=None, *, csrf=True, cookie=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if csrf:
            req.add_header(CSRF_HEADER, "1")
        if cookie and self.cookie:
            req.add_header("Cookie", f"{COOKIE_NAME}={self.cookie}")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw, status = response.read(), response.status
                self.last_set_cookie = response.headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
            self.last_set_cookie = exc.headers.get("Set-Cookie")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"_raw": raw}
        if self.last_set_cookie:
            jar = SimpleCookie()
            jar.load(self.last_set_cookie)
            morsel = jar.get(COOKIE_NAME)
            if morsel and morsel.value:
                self.cookie = morsel.value
        return status, payload

    def login(self, token=TOKEN):
        return self.request("POST", "/api/login", {"token": token}, csrf=False)


class WebTestCase(unittest.TestCase):
    """Boots a server per test with an injectable planner."""

    planner_responses = (PlannerResponse(text="Done."),)
    tool_risk = Risk.REVERSIBLE

    def setUp(self):
        self.tool_calls: list[dict] = []
        self.planner = ScriptedPlanner(*self.planner_responses)
        self.audit = MemoryAuditSink()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        runtime = AgentRuntime(
            planner=self.planner,
            registry=ToolRegistry([make_tool(risk=self.tool_risk, calls=self.tool_calls)]),
            policy=DefaultPolicy(str(OWNER)),
            system_prompt="Use tools when needed.",
            audit=self.audit,
        )
        self.sessions = SqliteSessionStore(str(Path(self.tmp.name) / "s.db"))
        self.addCleanup(self.sessions.close)
        self.archive = ChatArchive(str(Path(self.tmp.name) / "chats.db"), max_conversations=3)
        self.addCleanup(self.archive.close)
        self.app = WebApp(
            runtime=runtime,
            approvals=InMemoryApprovalStore(ttl_seconds=300),
            sessions=self.sessions,
            archive=self.archive,
            auth=AuthManager(TOKEN),
            owner_id=OWNER,
            session_ttl_seconds=900,
        )
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_address[1]}")


class TokenValidationTests(unittest.TestCase):
    def test_missing_token_is_rejected_with_guidance(self):
        with self.assertRaises(WebConfigError) as ctx:
            validate_token(None)
        self.assertIn("ANDROID_AGENT_WEB_TOKEN", str(ctx.exception))

    def test_short_token_is_rejected(self):
        with self.assertRaises(WebConfigError):
            validate_token("short")

    def test_placeholder_token_is_rejected(self):
        for placeholder in ("changeme", "password", "your-token-here"):
            with self.subTest(placeholder=placeholder), self.assertRaises(WebConfigError):
                validate_token(placeholder)

    def test_strong_token_is_accepted_and_stripped(self):
        self.assertEqual(validate_token(f"  {TOKEN}  "), TOKEN)


class AuthTests(WebTestCase):
    def test_api_requires_authentication(self):
        status, payload = self.client.request("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertNotIn("level", json.dumps(payload))

    def test_wrong_token_is_rejected(self):
        status, payload = self.client.login("w" * 24)
        self.assertEqual(status, 401)
        self.assertIsNone(self.client.cookie)
        self.assertEqual(payload["error"], "Invalid access token.")

    def test_login_sets_a_hardened_cookie(self):
        status, _ = self.client.login()
        self.assertEqual(status, 200)
        cookie = self.client.last_set_cookie
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)

    def test_session_id_is_not_derived_from_the_token(self):
        self.client.login()
        self.assertNotIn(TOKEN, self.client.cookie or "")
        self.assertGreaterEqual(len(self.client.cookie or ""), 16)

    def test_authenticated_state_is_served(self):
        self.client.login()
        status, payload = self.client.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["tools"], 1)
        self.assertFalse(payload["session"]["active"])

    def test_logout_invalidates_the_session(self):
        self.client.login()
        self.client.request("POST", "/api/logout")
        status, _ = self.client.request("GET", "/api/state")
        self.assertEqual(status, 401)

    def test_forged_session_id_is_rejected(self):
        self.client.login()
        self.client.cookie = "f" * 32
        status, _ = self.client.request("GET", "/api/state")
        self.assertEqual(status, 401)

    def test_repeated_failures_lock_the_client_out(self):
        for _ in range(5):
            self.client.login("w" * 24)
        status, payload = self.client.login("w" * 24)
        self.assertEqual(status, 429)
        self.assertIn("Locked out", payload["error"])

    def test_lockout_blocks_even_the_correct_token(self):
        for _ in range(5):
            self.client.login("w" * 24)
        status, _ = self.client.login(TOKEN)
        self.assertEqual(status, 429)
        self.assertIsNone(self.client.cookie)


class CsrfTests(WebTestCase):
    def test_missing_custom_header_blocks_state_reads(self):
        self.client.login()
        status, payload = self.client.request("GET", "/api/state", csrf=False)
        self.assertEqual(status, 403)
        self.assertNotIn("messages", payload)

    def test_missing_custom_header_blocks_messages(self):
        self.client.login()
        status, _ = self.client.request(
            "POST", "/api/message", {"text": "set volume to 8"}, csrf=False
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.tool_calls, [])


class MessageTests(WebTestCase):
    planner_responses = (
        PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),)),
        PlannerResponse(text="Volume is now 8."),
    )

    def test_message_runs_the_tool_and_persists_the_session(self):
        self.client.login()
        status, payload = self.client.request("POST", "/api/message", {"text": "volume 8"})
        self.assertEqual(status, 200)
        self.assertEqual(self.tool_calls, [{"level": 8}])
        roles = [entry["role"] for entry in payload["state"]["messages"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(payload["state"]["messages"][-1]["text"], "Volume is now 8.")
        self.assertTrue(payload["state"]["session"]["active"])
        self.assertEqual(payload["state"]["session"]["turns"], 1)

    def test_second_turn_carries_the_first_turn_into_the_prompt(self):
        self.client.login()
        self.client.request("POST", "/api/message", {"text": "volume 8"})
        self.planner.requests.clear()
        self.client.request("POST", "/api/message", {"text": "and again"})
        replayed = json.dumps(self.planner.requests[0])
        self.assertIn("volume 8", replayed)

    def test_new_clears_transcript_and_session(self):
        self.client.login()
        self.client.request("POST", "/api/message", {"text": "volume 8"})
        status, payload = self.client.request("POST", "/api/new")
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"]["messages"], [])
        self.assertFalse(payload["state"]["session"]["active"])

    def test_new_session_does_not_replay_old_history(self):
        self.client.login()
        self.client.request("POST", "/api/message", {"text": "volume 8"})
        self.client.request("POST", "/api/new")
        self.planner.requests.clear()
        self.client.request("POST", "/api/message", {"text": "fresh start"})
        self.assertNotIn("volume 8", json.dumps(self.planner.requests[0]))

    def test_web_and_telegram_histories_stay_separate(self):
        self.client.login()
        self.client.request("POST", "/api/message", {"text": "volume 8"})
        self.assertIsNone(self.sessions.active(OWNER))
        self.assertIsNotNone(self.sessions.active(self.app.session_key))

    def test_empty_message_is_rejected(self):
        self.client.login()
        status, _ = self.client.request("POST", "/api/message", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(self.tool_calls, [])

    def test_oversized_message_is_rejected(self):
        self.client.login()
        status, _ = self.client.request("POST", "/api/message", {"text": "x" * 5000})
        self.assertEqual(status, 400)
        self.assertEqual(self.tool_calls, [])

    def test_unknown_endpoint_returns_404(self):
        self.client.login()
        status, _ = self.client.request("POST", "/api/nope", {})
        self.assertEqual(status, 404)


class ConversationTests(WebTestCase):
    """Recent chats: archived transcripts, reopening, and deletion."""

    planner_responses = (PlannerResponse(text="Done."),)

    def setUp(self):
        super().setUp()
        self.client.login()

    def _titles(self, state):
        return [c["title"] for c in state["conversations"]]

    def test_a_chat_is_archived_as_soon_as_it_starts(self):
        _, payload = self.client.request("POST", "/api/message", {"text": "first thing"})
        self.assertEqual(self._titles(payload["state"]), ["first thing"])
        self.assertTrue(payload["state"]["conversations"][0]["current"])

    def test_new_chat_archives_the_previous_one(self):
        self.client.request("POST", "/api/message", {"text": "older chat"})
        _, payload = self.client.request("POST", "/api/new")
        self.assertEqual(payload["state"]["messages"], [])
        self.assertEqual(self._titles(payload["state"]), ["older chat"])
        self.assertFalse(payload["state"]["conversations"][0]["current"])

    def test_empty_chats_never_appear_in_the_list(self):
        self.client.request("POST", "/api/new")
        self.client.request("POST", "/api/new")
        _, payload = self.client.request("GET", "/api/state")
        self.assertEqual(payload["conversations"], [])

    def test_reopening_restores_the_transcript(self):
        self.client.request("POST", "/api/message", {"text": "remember this"})
        _, after_new = self.client.request("POST", "/api/new")
        old_id = after_new["state"]["conversations"][0]["id"]
        status, payload = self.client.request("POST", "/api/conversations/open", {"id": old_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"]["messages"][0]["text"], "remember this")
        self.assertEqual(payload["state"]["conversation_id"], old_id)

    def test_reopening_does_not_restore_model_context(self):
        """The transcript is a record; the model's memory still expired."""
        self.client.request("POST", "/api/message", {"text": "secret contact lookup"})
        _, after_new = self.client.request("POST", "/api/new")
        old_id = after_new["state"]["conversations"][0]["id"]
        _, payload = self.client.request("POST", "/api/conversations/open", {"id": old_id})
        self.assertTrue(payload["state"]["context_lost"])
        self.assertIsNone(self.sessions.active(self.app.session_key))

        self.planner.requests.clear()
        self.client.request("POST", "/api/message", {"text": "what did I just ask?"})
        self.assertNotIn("secret contact lookup", json.dumps(self.planner.requests[0]))

    def test_switching_away_from_a_live_chat_drops_its_context(self):
        """Opening chat B must not leave chat A's tool output in the prompt."""
        self.client.request("POST", "/api/message", {"text": "chat A private data"})
        self.client.request("POST", "/api/new")
        _, second = self.client.request("POST", "/api/message", {"text": "chat B"})
        # Chat B is live and remembered at this point.
        self.assertTrue(second["state"]["session"]["active"])
        older = next(c["id"] for c in second["state"]["conversations"] if not c["current"])

        _, opened = self.client.request("POST", "/api/conversations/open", {"id": older})
        self.assertTrue(opened["state"]["context_lost"])
        self.assertIsNone(self.sessions.active(self.app.session_key))

        self.planner.requests.clear()
        self.client.request("POST", "/api/message", {"text": "continue"})
        replayed = json.dumps(self.planner.requests[0])
        self.assertNotIn("chat A private data", replayed)
        self.assertNotIn("chat B", replayed)

    def test_context_lost_is_false_for_a_live_conversation(self):
        _, payload = self.client.request("POST", "/api/message", {"text": "live one"})
        self.assertFalse(payload["state"]["context_lost"])

    def test_opening_a_missing_chat_returns_404(self):
        status, _ = self.client.request("POST", "/api/conversations/open", {"id": "ghost"})
        self.assertEqual(status, 404)

    def test_deleting_the_current_chat_clears_the_screen(self):
        _, payload = self.client.request("POST", "/api/message", {"text": "delete me"})
        current = payload["state"]["conversation_id"]
        _, after = self.client.request("POST", "/api/conversations/delete", {"id": current})
        self.assertEqual(after["state"]["messages"], [])
        self.assertEqual(after["state"]["conversations"], [])
        self.assertNotEqual(after["state"]["conversation_id"], current)

    def test_deleting_another_chat_leaves_the_current_one_alone(self):
        self.client.request("POST", "/api/message", {"text": "old chat"})
        _, after_new = self.client.request("POST", "/api/new")
        old_id = after_new["state"]["conversations"][0]["id"]
        self.client.request("POST", "/api/message", {"text": "current chat"})
        _, after = self.client.request("POST", "/api/conversations/delete", {"id": old_id})
        self.assertEqual(self._titles(after["state"]), ["current chat"])
        self.assertEqual(after["state"]["messages"][0]["text"], "current chat")

    def test_clear_all_wipes_the_archive_and_the_session(self):
        self.client.request("POST", "/api/message", {"text": "one"})
        self.client.request("POST", "/api/new")
        self.client.request("POST", "/api/message", {"text": "two"})
        status, payload = self.client.request("POST", "/api/conversations/clear")
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], 2)
        self.assertEqual(payload["state"]["conversations"], [])
        self.assertEqual(payload["state"]["messages"], [])
        self.assertIsNone(self.sessions.active(self.app.session_key))

    def test_archive_is_capped_and_old_chats_are_really_deleted(self):
        ids = []
        for index in range(5):
            _, payload = self.client.request("POST", "/api/message", {"text": f"chat {index}"})
            ids.append(payload["state"]["conversation_id"])
            self.client.request("POST", "/api/new")
        _, state = self.client.request("GET", "/api/state")
        self.assertEqual(self._titles(state), ["chat 4", "chat 3", "chat 2"])
        # Not merely hidden from the list: the rows are gone from disk.
        self.assertIsNone(self.archive.transcript(ids[0]))
        self.assertIsNone(self.archive.transcript(ids[1]))
        self.assertIsNotNone(self.archive.transcript(ids[2]))

    def test_conversation_endpoints_require_authentication(self):
        self.client.request("POST", "/api/logout")
        for path in ("/api/conversations/open", "/api/conversations/delete",
                     "/api/conversations/clear"):
            with self.subTest(path=path):
                status, _ = self.client.request("POST", path, {"id": "x"})
                self.assertEqual(status, 401)

    def test_transcript_survives_a_restart(self):
        self.client.request("POST", "/api/message", {"text": "persisted line"})
        rebuilt = WebApp(
            runtime=self.app.runtime,
            approvals=self.app.approvals,
            sessions=self.sessions,
            archive=self.archive,
            auth=AuthManager(TOKEN),
            owner_id=OWNER,
            session_ttl_seconds=900,
        )
        self.assertEqual(rebuilt.transcript[0]["text"], "persisted line")
        self.assertEqual(rebuilt.conversation_id, self.app.conversation_id)


class ApprovalTests(WebTestCase):
    """Dangerous tools must not execute on the web path without a click."""

    tool_risk = Risk.EXTERNAL_SIDE_EFFECT
    planner_responses = (
        PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),)),
        PlannerResponse(text="Done."),
    )

    def _pending_id(self, payload):
        for entry in payload["state"]["messages"]:
            if entry["role"] == "approval":
                return entry["approval_id"]
        raise AssertionError("no approval was surfaced")

    def test_dangerous_call_waits_for_approval(self):
        self.client.login()
        _, payload = self.client.request("POST", "/api/message", {"text": "volume 8"})
        self.assertEqual(self.tool_calls, [])
        self._pending_id(payload)

    def test_approving_executes_the_call(self):
        self.client.login()
        _, payload = self.client.request("POST", "/api/message", {"text": "volume 8"})
        approval_id = self._pending_id(payload)
        status, after = self.client.request(
            "POST", "/api/approval", {"approval_id": approval_id, "approve": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.tool_calls, [{"level": 8}])
        self.assertEqual(after["state"]["messages"][-1]["text"], "set_volume done")

    def test_denying_does_not_execute_the_call(self):
        self.client.login()
        _, payload = self.client.request("POST", "/api/message", {"text": "volume 8"})
        approval_id = self._pending_id(payload)
        self.client.request(
            "POST", "/api/approval", {"approval_id": approval_id, "approve": False}
        )
        self.assertEqual(self.tool_calls, [])

    def test_approval_cannot_be_replayed(self):
        self.client.login()
        _, payload = self.client.request("POST", "/api/message", {"text": "volume 8"})
        approval_id = self._pending_id(payload)
        body = {"approval_id": approval_id, "approve": True}
        self.client.request("POST", "/api/approval", body)
        self.client.request("POST", "/api/approval", body)
        self.assertEqual(self.tool_calls, [{"level": 8}])

    def test_unknown_approval_id_is_harmless(self):
        self.client.login()
        status, _ = self.client.request(
            "POST", "/api/approval", {"approval_id": "nope", "approve": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.tool_calls, [])

    def test_approval_requires_authentication(self):
        status, _ = self.client.request(
            "POST", "/api/approval", {"approval_id": "x", "approve": True}
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.tool_calls, [])


class MediaTests(WebTestCase):
    def test_traversal_attempts_are_refused(self):
        self.client.login()
        for name in ("../../etc/passwd", "..%2Fsecret", ".hidden", "", "a/b.png"):
            with self.subTest(name=name):
                status, _ = self.client.request("GET", f"/api/media/file?name={name}")
                self.assertIn(status, (400, 404))

    def test_media_index_requires_authentication(self):
        status, _ = self.client.request("GET", "/api/media")
        self.assertEqual(status, 401)


class StaticTests(WebTestCase):
    def test_ui_is_served_without_a_session(self):
        status, payload = self.client.request("GET", "/", csrf=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", payload["_raw"][:200].lower())

    def test_ui_does_not_embed_the_token(self):
        _, payload = self.client.request("GET", "/", csrf=False)
        self.assertNotIn(TOKEN.encode(), payload["_raw"])

    def test_ping_reports_auth_state(self):
        status, payload = self.client.request("GET", "/api/ping", csrf=False)
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.client.login()
        _, payload = self.client.request("GET", "/api/ping", csrf=False)
        self.assertTrue(payload["authenticated"])

    def test_ui_is_self_contained(self):
        """No CDN, no webfont, no tracker: the phone may be offline."""
        html = UI_PATH.read_text(encoding="utf-8")
        for pattern in ("src=\"http", "href=\"http://", "cdn.", "googleapis", "unpkg"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, html)

    def test_ui_ships_both_themes(self):
        html = UI_PATH.read_text(encoding="utf-8")
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn('data-theme="dark"', html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("color-scheme: light dark", html)

    def test_unknown_get_path_returns_404(self):
        status, _ = self.client.request("GET", "/secret", csrf=False)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
