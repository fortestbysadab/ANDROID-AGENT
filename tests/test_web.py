"""Tests for the local web console.

These run a real ThreadingHTTPServer on an ephemeral port and talk to it over
real HTTP, because most of what matters here (cookies, custom headers, status
codes, path handling) lives in the HTTP layer and would be invisible to a test
that called the handler methods directly.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

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

    def head(self, path):
        """Return response headers for an authenticated GET."""
        req = urllib.request.Request(self.base + path, method="GET")
        if self.cookie:
            req.add_header("Cookie", f"{COOKIE_NAME}={self.cookie}")
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            return dict(response.headers)

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
    def _media_file(self, name="shot.png", body=b"\x89PNG\r\n\x1a\nfake"):
        root = Path(self.tmp.name) / "media"
        (root / "screenshots").mkdir(parents=True, exist_ok=True)
        (root / "screenshots" / name).write_bytes(body)
        patcher = mock.patch("android_agent.web.server.media_root", return_value=root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return name, body

    def test_opening_a_media_file_in_a_new_tab_works(self):
        """A tapped photo is a top-level navigation: no custom header exists."""
        name, body = self._media_file()
        self.client.login()
        status, payload = self.client.request(
            "GET", f"/api/media/file?name={name}", csrf=False
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["_raw"], body)

    def test_media_file_still_requires_a_session(self):
        name, _ = self._media_file()
        status, payload = self.client.request(
            "GET", f"/api/media/file?name={name}", csrf=False
        )
        self.assertEqual(status, 401)
        self.assertNotIn("_raw", payload)

    def test_media_file_is_served_inline_and_not_sniffable(self):
        name, _ = self._media_file()
        self.client.login()
        headers = self.client.head(f"/api/media/file?name={name}")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("inline", headers.get("Content-Disposition", ""))
        self.assertEqual(headers.get("Content-Type"), "image/png")

    def test_traversal_attempts_are_refused(self):
        self.client.login()
        for name in ("../../etc/passwd", "..%2Fsecret", ".hidden", "", "a/b.png"):
            with self.subTest(name=name):
                status, _ = self.client.request("GET", f"/api/media/file?name={name}")
                self.assertIn(status, (400, 404))

    def test_media_index_requires_authentication(self):
        status, _ = self.client.request("GET", "/api/media")
        self.assertEqual(status, 401)


class LocationArtifactTests(WebTestCase):
    """Coordinates reach the UI as numbers, not only as a sentence."""

    planner_responses = (PlannerResponse(text="Here you are."),)

    def _artifacts(self, data):
        handler = self.app  # WebApp owns no handler; use the helper directly
        from android_agent.tools.base import ToolResult
        from android_agent.web.server import Handler

        return Handler._artifacts(handler, [ToolResult.ok("Location.", data)])

    def test_a_location_result_carries_numeric_coordinates(self):
        artifacts = self._artifacts({"latitude": 22.36464, "longitude": 87.9995})
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["kind"], "location")
        self.assertAlmostEqual(artifacts[0]["latitude"], 22.36464)
        self.assertAlmostEqual(artifacts[0]["longitude"], 87.9995)

    def test_accuracy_is_included_when_known(self):
        artifacts = self._artifacts(
            {"latitude": 22.4, "longitude": 87.9, "accuracy": 8.0}
        )
        self.assertEqual(artifacts[0]["accuracy"], 8.0)

    def test_a_coarse_fix_is_flagged_for_the_ui(self):
        artifacts = self._artifacts(
            {"latitude": 22.4, "longitude": 87.9, "accuracy": 800.0, "approximate": True}
        )
        self.assertTrue(artifacts[0]["approximate"])

    def test_a_precise_fix_is_not_flagged(self):
        artifacts = self._artifacts(
            {"latitude": 22.4, "longitude": 87.9, "accuracy": 8.0, "approximate": False}
        )
        self.assertNotIn("approximate", artifacts[0])

    def test_a_result_without_coordinates_produces_no_location_artifact(self):
        self.assertEqual(self._artifacts({"percentage": 87}), [])

    def test_the_human_readable_name_is_kept(self):
        artifacts = self._artifacts({"latitude": 22.36464, "longitude": 87.9995})
        self.assertEqual(artifacts[0]["name"], "22.36464, 87.99950")


class WebSearchToggleTests(WebTestCase):
    """The composer toggle states intent; it does not force a tool call."""

    planner_responses = (PlannerResponse(text="Here is what I found."),)

    def setUp(self):
        super().setUp()
        self.client.login()

    def _prompt_sent(self):
        return self.planner.requests[0][-1]["content"]

    def test_a_plain_message_is_passed_through_untouched(self):
        self.client.request("POST", "/api/message", {"text": "hello there"})
        self.assertEqual(self._prompt_sent(), "hello there")

    def test_the_toggle_adds_an_instruction_to_search(self):
        self.client.request(
            "POST", "/api/message", {"text": "latest android news", "web_search": True}
        )
        prompt = self._prompt_sent()
        self.assertIn("latest android news", prompt)
        self.assertIn("web_search", prompt)
        self.assertIn("cite", prompt.lower())

    def test_the_transcript_records_what_the_owner_typed(self):
        """Not the augmented prompt: the owner never wrote that."""
        _, payload = self.client.request(
            "POST", "/api/message", {"text": "weather today", "web_search": True}
        )
        said = [m for m in payload["state"]["messages"] if m["role"] == "user"]
        self.assertEqual(said[-1]["text"], "weather today")

    def test_the_toggle_defaults_to_off(self):
        self.client.request("POST", "/api/message", {"text": "hello"})
        self.assertNotIn("web search", self._prompt_sent().lower())


class ScreenDataTests(WebTestCase):
    """The Files, Tools and Schedule screens read from these."""

    def setUp(self):
        super().setUp()
        self.client.login()

    def test_tools_are_listed_with_risk_and_description(self):
        status, payload = self.client.request("GET", "/api/tools")
        self.assertEqual(status, 200)
        tools = payload["tools"]
        self.assertTrue(tools)
        first = tools[0]
        for field in ("name", "risk", "description", "untrusted"):
            with self.subTest(field=field):
                self.assertIn(field, first)

    def test_tools_are_sorted_so_the_list_is_stable(self):
        _, payload = self.client.request("GET", "/api/tools")
        names = [tool["name"] for tool in payload["tools"]]
        self.assertEqual(names, sorted(names))

    def test_screenshots_and_photos_appear_alongside_documents(self):
        """The reported gap: captures were written somewhere the screen never read."""
        media = Path(self.tmp.name) / "media"
        files = media / "files"
        for folder in ("files", "photos", "screenshots", "recordings"):
            (media / folder).mkdir(parents=True, exist_ok=True)
        (files / "report.pdf").write_bytes(b"pdf")
        (media / "screenshots" / "shot.png").write_bytes(b"png")
        (media / "photos" / "snap.jpg").write_bytes(b"jpg")
        (media / "recordings" / "note.m4a").write_bytes(b"m4a")

        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=files
        ), mock.patch(
            "android_agent.tools.media.media_root", return_value=media
        ):
            _, payload = self.client.request("GET", "/api/files")
        by_name = {item["name"]: item for item in payload["files"]}
        self.assertEqual(
            set(by_name), {"report.pdf", "shot.png", "snap.jpg", "note.m4a"}
        )
        self.assertEqual(by_name["shot.png"]["kind"], "screenshot")
        self.assertEqual(by_name["snap.jpg"]["kind"], "photo")
        self.assertEqual(by_name["note.m4a"]["kind"], "recording")
        self.assertEqual(by_name["report.pdf"]["kind"], "document")

    def test_files_are_newest_first_across_every_folder(self):
        import os
        import time

        media = Path(self.tmp.name) / "media2"
        files = media / "files"
        for folder in ("files", "screenshots"):
            (media / folder).mkdir(parents=True, exist_ok=True)
        old = files / "old.pdf"
        new = media / "screenshots" / "new.png"
        old.write_bytes(b"a")
        new.write_bytes(b"b")
        os.utime(old, (time.time() - 3600,) * 2)

        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=files
        ), mock.patch(
            "android_agent.tools.media.media_root", return_value=media
        ):
            _, payload = self.client.request("GET", "/api/files")
        self.assertEqual([item["name"] for item in payload["files"]], ["new.png", "old.pdf"])

    def test_a_screenshot_can_be_downloaded(self):
        media = Path(self.tmp.name) / "media3"
        (media / "screenshots").mkdir(parents=True, exist_ok=True)
        (media / "files").mkdir(parents=True, exist_ok=True)
        (media / "screenshots" / "shot.png").write_bytes(b"\x89PNG-data")

        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=media / "files"
        ), mock.patch(
            "android_agent.tools.media.media_root", return_value=media
        ):
            status, payload = self.client.request(
                "GET", "/api/files/download?name=shot.png", csrf=False
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["_raw"], b"\x89PNG-data")

    def test_files_lists_the_folder_not_only_known_documents(self):
        """Files the owner dropped in by hand must be visible too."""
        folder = Path(self.tmp.name) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "by-hand.txt").write_text("hello", encoding="utf-8")
        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=folder
        ):
            status, payload = self.client.request("GET", "/api/files")
        self.assertEqual(status, 200)
        self.assertEqual([f["name"] for f in payload["files"]], ["by-hand.txt"])
        self.assertEqual(payload["files"][0]["size"], 5)

    def test_files_is_empty_rather_than_failing_with_no_folder(self):
        missing = Path(self.tmp.name) / "nope"
        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=missing
        ):
            status, payload = self.client.request("GET", "/api/files")
        self.assertEqual(status, 200)
        self.assertEqual(payload["files"], [])

    def test_schedule_is_empty_without_a_store(self):
        status, payload = self.client.request("GET", "/api/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tasks"], [])

    def test_the_screen_endpoints_need_authentication(self):
        self.client.request("POST", "/api/logout")
        for path in ("/api/files", "/api/tools", "/api/schedule"):
            with self.subTest(path=path):
                status, _ = self.client.request("GET", path)
                self.assertEqual(status, 401)

    def test_a_file_can_be_downloaded_by_name(self):
        folder = Path(self.tmp.name) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.txt").write_bytes(b"content here")
        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=folder
        ):
            status, payload = self.client.request(
                "GET", "/api/files/download?name=report.txt", csrf=False
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["_raw"], b"content here")

    def test_a_download_cannot_escape_the_files_folder(self):
        folder = Path(self.tmp.name) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "android_agent.documents.store.documents_root", return_value=folder
        ):
            for attempt in ("../secret", "/etc/passwd", "..%2Fsecret"):
                with self.subTest(attempt=attempt):
                    status, _ = self.client.request(
                        "GET",
                        f"/api/files/download?name={urllib.parse.quote(attempt)}",
                        csrf=False,
                    )
                    self.assertIn(status, (400, 403, 404))


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

    def test_ui_loads_nothing_external(self):
        """No CDN, no webfont, no tracker: the phone may be offline.

        The map is the single deliberate exception and is *not* covered by
        this test, because it is never fetched on load - see
        test_the_map_is_only_fetched_when_the_owner_asks.
        """
        html = UI_PATH.read_text(encoding="utf-8")
        for pattern in ("src=\"http", "href=\"http://", "cdn.", "googleapis", "unpkg"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, html)

    def test_the_map_is_only_fetched_when_the_owner_asks(self):
        """One external resource exists; it must stay opt-in.

        A map embedded in the markup would make every page load call Google,
        and would leave a broken frame on a phone with no connection. It is
        therefore created only after a click. Asserted as a property of the
        delivered document rather than of any one implementation, since the
        console has been both hand-written and React.
        """
        html = UI_PATH.read_text(encoding="utf-8")
        self.assertIn("maps.google.com", html, "the map affordance is missing")
        self.assertNotRegex(
            html, r"src=\s*[\"']https://maps", "the map must not load on page open"
        )
        self.assertNotIn("<iframe", html, "no iframe may exist in the markup")
        self.assertIn("Show map", html, "there must be something to click")

    def test_coordinates_still_show_without_the_map(self):
        """Offline, the card must still answer 'where am I'."""
        html = UI_PATH.read_text(encoding="utf-8")
        self.assertIn("Open in Maps", html)
        self.assertRegex(html, r"toFixed\(5\)")

    def test_ui_ships_both_themes(self):
        """Light and dark authored separately, plus an explicit override."""
        html = UI_PATH.read_text(encoding="utf-8")
        self.assertRegex(html, r"prefers-color-scheme:\s*dark")
        self.assertRegex(html, r"\[data-theme=[\"']?dark")
        self.assertIn("prefers-reduced-motion", html)
        self.assertRegex(html, r"color-scheme:\s*light dark")

    def test_the_theme_is_applied_before_first_paint(self):
        """Otherwise a dark-mode user gets a white flash on every load."""
        html = UI_PATH.read_text(encoding="utf-8")
        head = html.split("<div id=\"root\">")[0]
        self.assertIn("aa-theme", head)

    def test_the_ui_renders_content_as_data_not_markup(self):
        """Model output and message bodies must never become HTML."""
        # Matches use, not mention: a comment saying the app avoids this
        # should not fail the check that it avoids it.
        usage = re.compile(r"dangerouslySetInnerHTML\s*[=:]")
        for path in Path("web-ui/src").glob("*.jsx"):
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text("utf-8"), usage)

    def test_unknown_get_path_returns_404(self):
        status, _ = self.client.request("GET", "/secret", csrf=False)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
