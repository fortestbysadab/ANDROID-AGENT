"""Screen access through the installed companion app.

The companion runs an AccessibilityService, which is the only supported way
to read another app's screen and tap it without a host machine attached. It
listens on an abstract-namespace unix socket, the same mechanism Termux:API
uses, and speaks one JSON object per connection.

This backend satisfies the identical `Backend` protocol as `AdbBackend`, so
the workflows, limits, policy and audit trail above it are unchanged. Two
details differ underneath and are translated here:

* The companion addresses elements by node index, not by pixel. The protocol
  above speaks in coordinates, so taps are resolved back to the node whose
  bounds contain the point.
* An accessibility service cannot inject key events. Pressing enter becomes
  the editor action on the focused field; back and home become global
  actions. Anything else is refused rather than silently dropped.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from .models import AutomationError, UiElement, UiSnapshot

logger = logging.getLogger(__name__)

#: Abstract namespace: the leading NUL is what makes it abstract, so the
#: socket lives in the kernel rather than on a filesystem Termux cannot see.
SOCKET_NAME = "\0com.androidagent.companion.bridge"
PROTOCOL_VERSION = 1
TOKEN_VARIABLE = "ANDROID_AGENT_COMPANION_TOKEN"  # noqa: S105 - a variable name

CONNECT_TIMEOUT = 4.0
READ_TIMEOUT = 12.0
#: A dump of a real screen is tens of kilobytes; far beyond that is a fault.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_NODES = 300
MAX_NODE_TEXT = 200

#: Companion error codes that mean "the app is there but refused", mapped to
#: the vocabulary the rest of the system already uses.
_CODE_MAP = {
    "unauthorised": "backend_unavailable",
    "version_mismatch": "backend_unavailable",
    "no_window": "unsupported_screen",
    "stale_reference": "stale_snapshot",
    "not_actionable": "unsupported_screen",
    "action_failed": "verification_failed",
    "app_not_installed": "app_not_installed",
    "unsupported": "unsupported_screen",
    "invalid_argument": "invalid_parameters",
    "unknown_op": "unsupported_screen",
    "timeout": "state_timeout",
    "internal_error": "unsupported_screen",
    "bad_request": "invalid_parameters",
}


def _unavailable(detail: str) -> AutomationError:
    return AutomationError(detail, code="backend_unavailable")


class CompanionBackend:
    """Screen access via the companion app's accessibility service."""

    name = "companion"

    def __init__(self, token: str | None = None, *, connect=None) -> None:
        self._token = token if token is not None else os.environ.get(TOKEN_VARIABLE, "")
        self._connect = connect or self._open_socket
        #: The dump the current element ids came from. The companion refuses
        #: an index carrying a stale sequence, so a reference that outlives
        #: its screen fails loudly instead of landing on a different node.
        self._seq: int | None = None
        self._nodes: list[dict[str, Any]] = []
        #: Set by `tap`, consumed by `type_text`, because the protocol above
        #: types into "whatever was just tapped".
        self._focus: int | None = None

    # -- transport ----------------------------------------------------

    def _open_socket(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(CONNECT_TIMEOUT)
        client.connect(SOCKET_NAME)
        client.settimeout(READ_TIMEOUT)
        return client

    def request(self, op: str, **args: Any) -> dict[str, Any]:
        if not self._token:
            raise _unavailable(
                "The companion app is not paired. Open Android Agent Companion "
                f"on the device and copy its {TOKEN_VARIABLE} line into .env."
            )
        payload = json.dumps(
            {"v": PROTOCOL_VERSION, "token": self._token, "op": op, "args": args},
            ensure_ascii=False,
        ).encode() + b"\n"

        try:
            client = self._connect()
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            raise _unavailable(
                "The companion app is not listening. Open it and check that "
                "Android Agent is switched on under Accessibility."
            ) from exc

        try:
            with client:
                client.sendall(payload)
                raw = self._read_line(client)
        except TimeoutError as exc:
            raise AutomationError(
                "The companion app did not answer in time.",
                code="unsupported_screen",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise _unavailable(f"The companion connection failed: {exc}") from exc

        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise AutomationError(
                "The companion app sent a reply that could not be read.",
                code="unsupported_screen",
            ) from exc

        if not reply.get("ok"):
            code = str(reply.get("code", ""))
            message = str(reply.get("error", "The companion refused the request."))
            if code == "unauthorised":
                message = (
                    "The companion app rejected the pairing token. Reset it in "
                    f"the app and update {TOKEN_VARIABLE} in .env."
                )
            raise AutomationError(message, code=_CODE_MAP.get(code, "unsupported_screen"))
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _read_line(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise AutomationError(
                    "The companion app sent an implausibly large reply.",
                    code="unsupported_screen",
                )
            if chunk.endswith(b"\n"):
                break
        if not chunks:
            raise _unavailable("The companion app closed the connection without replying.")
        return b"".join(chunks)

    # -- Backend protocol ---------------------------------------------

    def available(self) -> tuple[bool, str]:
        if not self._token:
            return False, (
                "The companion app is not paired. Open Android Agent Companion "
                f"on the device, copy its {TOKEN_VARIABLE} line into .env and "
                "restart the agent."
            )
        try:
            self.request("ping")
        except AutomationError as exc:
            return False, str(exc)
        return True, ""

    def launch(self, package: str) -> None:
        self.request("launch", package=package)

    def open_url(self, url: str) -> None:
        # The companion has no intent entry point on purpose: a general
        # "start this intent" call would be a far wider hole than anything
        # else here. Opening a URL stays with the tool that already does it
        # under the owner's own permissions.
        import shutil
        import subprocess

        if not shutil.which("termux-open-url"):
            raise _unavailable(
                "termux-open-url is missing, so a link cannot be opened. "
                "Install Termux:API with `pkg install termux-api`."
            )
        try:
            completed = subprocess.run(
                ["termux-open-url", url],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AutomationError(
                "Opening the link timed out.", code="state_timeout", retryable=True
            ) from exc
        if completed.returncode != 0:
            raise AutomationError(
                (completed.stderr or "The link could not be opened.").strip()[:300],
                code="verification_failed",
            )

    def snapshot(self) -> UiSnapshot:
        data = self.request("dump")
        self._seq = data.get("seq")
        raw_elements = data.get("elements") or []
        self._nodes = [node for node in raw_elements if isinstance(node, dict)]
        self._focus = None

        snapshot_id = UiSnapshot.new_id()
        elements: list[UiElement] = []
        width = height = 0
        truncated = bool(data.get("truncated"))

        for node in self._nodes:
            if len(elements) >= MAX_NODES:
                truncated = True
                break
            bounds = self._bounds(node)
            width = max(width, bounds[2])
            height = max(height, bounds[3])
            text = str(node.get("text", ""))[:MAX_NODE_TEXT]
            description = str(node.get("desc", ""))[:MAX_NODE_TEXT]
            resource_id = str(node.get("id", ""))[:MAX_NODE_TEXT]
            clickable = bool(node.get("clickable"))
            # Same rule as the ADB backend: an unlabelled node with no
            # affordance is layout scaffolding, not something to show a model.
            if not (text or description or resource_id) and not clickable:
                continue
            elements.append(UiElement(
                element_id=f"{snapshot_id}.{int(node.get('index', 0))}",
                text=text,
                description=description,
                resource_id=resource_id,
                class_name=str(node.get("cls", "")),
                clickable=clickable,
                editable=bool(node.get("editable")),
                enabled=bool(node.get("enabled", True)),
                bounds=bounds,
            ))

        return UiSnapshot(
            snapshot_id=snapshot_id,
            package=str(data.get("package", "")),
            elements=tuple(elements),
            width=width or 1080,
            height=height or 1920,
            truncated=truncated,
        )

    @staticmethod
    def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int]:
        raw = node.get("bounds") or []
        if not isinstance(raw, list) or len(raw) != 4:
            return (0, 0, 0, 0)
        try:
            left, top, right, bottom = (int(value) for value in raw)
        except (TypeError, ValueError):
            return (0, 0, 0, 0)
        return (left, top, right, bottom)

    def _node_at(self, x: int, y: int) -> int:
        """The smallest node containing the point.

        Smallest wins because containers enclose their children, and the
        caller means the specific control it measured, not the window.
        """
        best: int | None = None
        best_area = None
        for node in self._nodes:
            left, top, right, bottom = self._bounds(node)
            if not (left <= x < right and top <= y < bottom):
                continue
            area = (right - left) * (bottom - top)
            if best_area is None or area < best_area:
                best, best_area = int(node.get("index", 0)), area
        if best is None:
            raise AutomationError(
                "That control is no longer on screen.", code="stale_snapshot"
            )
        return best

    def tap(self, x: int, y: int) -> None:
        index = self._node_at(int(x), int(y))
        self._focus = index
        self.request("click", seq=self._seq, index=index)

    def type_text(self, text: str) -> None:
        if self._focus is None:
            raise AutomationError(
                "No text field has been selected to type into.",
                code="unsupported_screen",
            )
        self.request("set_text", seq=self._seq, index=self._focus, text=text)

    def scroll(self, direction: str, width: int, height: int) -> None:
        # "down" means the user wants to see further down the content, which
        # is a forward scroll. Width and height are unused: the companion
        # scrolls the scrollable node rather than swiping at coordinates.
        forward = direction != "up"
        self.request("scroll", direction="forward" if forward else "backward")

    def press(self, key: str) -> None:
        if key in ("enter", "search"):
            self.request("ime_enter", **({"seq": self._seq, "index": self._focus}
                                         if self._focus is not None else {}))
            return
        if key in ("back", "home", "recents", "notifications"):
            self.request("global", action=key)
            return
        raise AutomationError(
            f"The companion app cannot press {key!r}.", code="unsupported_screen"
        )
