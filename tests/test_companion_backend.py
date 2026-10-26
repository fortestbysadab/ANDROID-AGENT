"""Tests for the companion-app backend.

These drive a fake socket rather than a device. The point is the translation
layer: coordinates to node indices, enter to an editor action, companion
error codes to the vocabulary the rest of the system uses.
"""

from __future__ import annotations

import json

import pytest

from android_agent.automation.companion import CompanionBackend
from android_agent.automation.models import AutomationError


class FakeSocket:
    """One scripted reply per connection, recording what was sent."""

    def __init__(self, owner: FakeTransport) -> None:
        self._owner = owner
        self._reply = b""

    def sendall(self, payload: bytes) -> None:
        request = json.loads(payload.decode())
        self._owner.sent.append(request)
        self._reply = self._owner.next_reply(request)

    def recv(self, _size: int) -> bytes:
        reply, self._reply = self._reply, b""
        return reply

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeTransport:
    def __init__(self, replies=None) -> None:
        self.sent: list[dict] = []
        self.replies = list(replies or [])

    def next_reply(self, request: dict) -> bytes:
        if self.replies:
            reply = self.replies.pop(0)
        else:
            reply = {"ok": True, "data": {}}
        if callable(reply):
            reply = reply(request)
        return json.dumps(reply).encode() + b"\n"

    def __call__(self) -> FakeSocket:
        return FakeSocket(self)


def node(index, left, top, right, bottom, **extra):
    base = {
        "index": index,
        "text": "",
        "desc": "",
        "id": "",
        "cls": "android.widget.TextView",
        "clickable": True,
        "editable": False,
        "enabled": True,
        "bounds": [left, top, right, bottom],
    }
    base.update(extra)
    return base


def dump_reply(nodes, seq=7, package="com.example"):
    return {
        "ok": True,
        "data": {"seq": seq, "package": package, "truncated": False, "elements": nodes},
    }


def make(replies):
    transport = FakeTransport(replies)
    return CompanionBackend(token="secret", connect=transport), transport  # noqa: S106 - fake


# -- pairing ----------------------------------------------------------


def test_without_a_token_the_backend_reports_unavailable_and_sends_nothing():
    transport = FakeTransport()
    backend = CompanionBackend(token="", connect=transport)

    ready, detail = backend.available()

    assert ready is False
    assert "not paired" in detail
    assert transport.sent == [], "no request should be attempted without a token"


def test_every_request_carries_the_token_and_protocol_version():
    backend, transport = make([{"ok": True, "data": {"ready": True}}])

    backend.available()

    assert transport.sent[0]["token"] == "secret"  # noqa: S105 - fake
    assert transport.sent[0]["v"] == 1
    assert transport.sent[0]["op"] == "ping"


def test_a_rejected_token_tells_the_owner_how_to_re_pair():
    backend, _ = make([{"ok": False, "code": "unauthorised", "error": "nope"}])

    ready, detail = backend.available()

    assert ready is False
    assert "ANDROID_AGENT_COMPANION_TOKEN" in detail


# -- snapshots --------------------------------------------------------


def test_snapshot_keeps_labelled_nodes_and_drops_bare_scaffolding():
    nodes = [
        node(0, 0, 0, 1080, 200, text="Search"),
        node(1, 0, 200, 1080, 400, clickable=False),  # no label, no affordance
        node(2, 0, 400, 1080, 600, desc="Profile photo"),
    ]
    backend, _ = make([dump_reply(nodes)])

    snapshot = backend.snapshot()

    assert [element.label for element in snapshot.elements] == ["Search", "Profile photo"]
    assert snapshot.package == "com.example"


def test_element_ids_are_scoped_to_their_snapshot():
    nodes = [node(0, 0, 0, 100, 100, text="One")]
    backend, _ = make([dump_reply(nodes), dump_reply(nodes, seq=8)])

    first = backend.snapshot()
    second = backend.snapshot()

    assert first.elements[0].element_id != second.elements[0].element_id, (
        "an id from an older screen must not resolve against a newer one"
    )


# -- taps -------------------------------------------------------------


def test_a_tap_resolves_to_the_smallest_node_containing_the_point():
    nodes = [
        node(0, 0, 0, 1080, 1920, text="Root container"),
        node(1, 100, 100, 500, 300, text="Button"),
    ]
    backend, transport = make([dump_reply(nodes), {"ok": True, "data": {"performed": True}}])
    backend.snapshot()

    backend.tap(200, 200)

    click = transport.sent[-1]
    assert click["op"] == "click"
    assert click["args"]["index"] == 1, "the button, not the window that encloses it"
    assert click["args"]["seq"] == 7


def test_a_tap_outside_every_node_is_refused_as_a_stale_screen():
    backend, _ = make([dump_reply([node(0, 0, 0, 100, 100, text="One")])])
    backend.snapshot()

    with pytest.raises(AutomationError) as caught:
        backend.tap(900, 900)

    assert caught.value.code == "stale_snapshot"


def test_typing_targets_the_field_that_was_just_tapped():
    nodes = [node(0, 0, 0, 800, 120, text="", id="q", editable=True)]
    backend, transport = make([
        dump_reply(nodes),
        {"ok": True, "data": {}},
        {"ok": True, "data": {}},
    ])
    backend.snapshot()
    backend.tap(400, 60)

    backend.type_text("weather in Haldia")

    typed = transport.sent[-1]
    assert typed["op"] == "set_text"
    assert typed["args"]["index"] == 0
    assert typed["args"]["text"] == "weather in Haldia"


def test_typing_before_any_field_is_selected_is_refused():
    backend, transport = make([])

    with pytest.raises(AutomationError):
        backend.type_text("hello")

    assert transport.sent == [], "nothing should reach the device"


# -- keys -------------------------------------------------------------


def test_enter_becomes_the_editor_action_because_keys_cannot_be_injected():
    backend, transport = make([
        dump_reply([node(0, 0, 0, 800, 120, id="q", editable=True)]),
        {"ok": True, "data": {}},
        {"ok": True, "data": {}},
    ])
    backend.snapshot()
    backend.tap(400, 60)

    backend.press("enter")

    assert transport.sent[-1]["op"] == "ime_enter"


def test_back_becomes_a_global_action():
    backend, transport = make([{"ok": True, "data": {}}])

    backend.press("back")

    assert transport.sent[-1]["op"] == "global"
    assert transport.sent[-1]["args"]["action"] == "back"


def test_an_unsupported_key_is_refused_rather_than_silently_dropped():
    backend, transport = make([])

    with pytest.raises(AutomationError):
        backend.press("volume_up")

    assert transport.sent == []


def test_scrolling_down_asks_for_a_forward_scroll():
    backend, transport = make([{"ok": True, "data": {}}])

    backend.scroll("down", 1080, 1920)

    assert transport.sent[-1]["args"]["direction"] == "forward"


# -- error translation ------------------------------------------------


@pytest.mark.parametrize(
    ("companion_code", "expected"),
    [
        ("stale_reference", "stale_snapshot"),
        ("no_window", "unsupported_screen"),
        ("app_not_installed", "app_not_installed"),
        ("action_failed", "verification_failed"),
        ("timeout", "state_timeout"),
    ],
)
def test_companion_codes_map_onto_the_shared_vocabulary(companion_code, expected):
    backend, _ = make([{"ok": False, "code": companion_code, "error": "no"}])

    with pytest.raises(AutomationError) as caught:
        backend.launch("com.example")

    assert caught.value.code == expected


def test_an_unreachable_socket_is_reported_as_the_service_being_off():
    def refuse():
        raise ConnectionRefusedError(111, "Connection refused")

    backend = CompanionBackend(token="secret", connect=refuse)  # noqa: S106 - fake

    ready, detail = backend.available()

    assert ready is False
    assert "Accessibility" in detail, "the owner needs to know which switch to flip"
