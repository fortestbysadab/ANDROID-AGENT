"""Backends that can see and touch the screen.

Two exist by design. The ADB backend works today on any device with
wireless debugging: `uiautomator dump` returns the same accessibility node
tree an accessibility service would see, so the capability does not depend
on shipping an Android app. A companion-app backend can be added later
behind this identical protocol, and would buy two things ADB cannot — no
re-enabling debugging after a reboot, and event-driven waiting.

Nothing above this layer knows which backend is in use. That is the point:
the workflow code, the limits, the policy and the audit trail are the same
either way.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ElementTree
from typing import Any, Protocol

from .models import AutomationError, UiElement, UiSnapshot, parse_bounds

logger = logging.getLogger(__name__)

#: A screen with more nodes than this is almost certainly a scrolling list;
#: the cap keeps a snapshot from filling the model's context.
MAX_NODES = 300
MAX_NODE_TEXT = 200
DUMP_PATH = "/data/local/tmp/android-agent-ui.xml"
#: A screen dump is bounded by what fits on a display; anything far larger
#: is a malformed or hostile document rather than a user interface.
MAX_DUMP_BYTES = 4 * 1024 * 1024
#: Apps the owner can ask for by name. A package is never taken from model
#: output: it picks an id from this list or the request is refused.
SUPPORTED_APPS: dict[str, str] = {
    "chrome": "com.android.chrome",
    "instagram": "com.instagram.android",
    "settings": "com.android.settings",
    "whatsapp": "com.whatsapp",
    "youtube": "com.google.android.youtube",
    "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps",
}


class Backend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def launch(self, package: str) -> None: ...
    def open_url(self, url: str) -> None: ...
    def snapshot(self) -> UiSnapshot: ...
    def tap(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def scroll(self, direction: str, width: int, height: int) -> None: ...
    def press(self, key: str) -> None: ...


def _clip(value: str | None) -> str:
    return (value or "").strip()[:MAX_NODE_TEXT]


def parse_hierarchy(xml: str) -> UiSnapshot:
    """Turn a uiautomator dump into a bounded, addressable snapshot.

    The text in this document is written by whatever app is on screen, so it
    is untrusted input. Two cheap defences before parsing: refuse a document
    with a DTD, which is how entity-expansion attacks are delivered, and cap
    the size, since a real screen cannot produce megabytes of nodes.
    """
    if len(xml) > MAX_DUMP_BYTES:
        raise AutomationError(
            "The screen description was implausibly large and was not read.",
            code="unsupported_screen",
        )
    lowered = xml[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise AutomationError(
            "The screen description contained a document type declaration, "
            "which a real screen dump never does.",
            code="unsupported_screen",
        )
    try:
        root = ElementTree.fromstring(xml)  # noqa: S314 - guarded above
    except ElementTree.ParseError as exc:
        raise AutomationError(
            "The screen could not be read.", code="unsupported_screen", retryable=True
        ) from exc

    snapshot_id = UiSnapshot.new_id()
    elements: list[UiElement] = []
    package = ""
    width = height = 0
    truncated = False

    for index, node in enumerate(root.iter("node")):
        if len(elements) >= MAX_NODES:
            truncated = True
            break
        attrib = node.attrib
        package = package or attrib.get("package", "")
        bounds = parse_bounds(attrib.get("bounds", ""))
        width = max(width, bounds[2])
        height = max(height, bounds[3])
        text = _clip(attrib.get("text"))
        description = _clip(attrib.get("content-desc"))
        resource_id = _clip(attrib.get("resource-id"))
        clickable = attrib.get("clickable") == "true"
        editable = attrib.get("class", "").endswith("EditText") or (
            attrib.get("focusable") == "true" and attrib.get("editable") == "true"
        )
        # A node with no label and no affordance is scaffolding, not content.
        if not (text or description or resource_id) and not clickable:
            continue
        elements.append(UiElement(
            # Scoped to this snapshot: an id from an older screen must fail
            # to resolve rather than land on whatever now sits at that index.
            element_id=f"{snapshot_id}.{index}",
            text=text,
            description=description,
            resource_id=resource_id,
            class_name=attrib.get("class", ""),
            clickable=clickable,
            editable=editable,
            enabled=attrib.get("enabled", "true") == "true",
            bounds=bounds,
        ))

    return UiSnapshot(
        snapshot_id=snapshot_id,
        package=package,
        elements=tuple(elements),
        width=width or 1080,
        height=height or 1920,
        truncated=truncated,
    )


class AdbBackend:
    """Screen access over wireless ADB. No companion app required."""

    name = "adb"

    def __init__(self, shell=None) -> None:
        if shell is None:
            from android_agent.tools.adb import _shell

            shell = _shell
        self._shell = shell

    def _run(self, arguments: list[str], *, timeout: float = 20.0) -> str:
        ok, output = self._shell(arguments, timeout)
        if not ok:
            lowered = output.lower()
            if "not connected" in lowered or ("device" in lowered and "not found" in lowered):
                raise AutomationError(
                    "Wireless ADB is not connected, so the screen cannot be read. "
                    "Reconnect it and try again.",
                    code="backend_unavailable",
                )
            raise AutomationError(output[:300], code="unsupported_screen", retryable=True)
        return output

    def available(self) -> tuple[bool, str]:
        from android_agent.tools.adb import _connected

        if not _connected():
            return False, (
                "Wireless ADB is not connected. On the device, open Developer "
                "options > Wireless debugging, then run "
                "`adb connect localhost:<port>` in Termux."
            )
        return True, ""

    def launch(self, package: str) -> None:
        self._run(["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])

    def open_url(self, url: str) -> None:
        # The URL is built by application code from a validated query, never
        # supplied by the model.
        self._run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])

    def snapshot(self) -> UiSnapshot:
        """Dump to a file and read it back.

        Piping uiautomator to stdout intermittently returns empty or
        truncated XML; writing then reading is slower and works every time.
        """
        last: AutomationError | None = None
        for attempt in range(2):
            try:
                self._run(["uiautomator", "dump", DUMP_PATH], timeout=25.0)
                xml = self._run(["cat", DUMP_PATH], timeout=20.0)
                if "<hierarchy" in xml:
                    return parse_hierarchy(xml)
                last = AutomationError(
                    "The screen dump was empty.", code="unsupported_screen", retryable=True
                )
            except AutomationError as exc:
                if exc.code == "backend_unavailable":
                    raise
                last = exc
            # "could not get idle state" happens on an animating screen.
            if attempt == 0:
                time.sleep(1.2)
        raise last or AutomationError(
            "The screen could not be read.", code="unsupported_screen"
        )

    def tap(self, x: int, y: int) -> None:
        self._run(["input", "tap", str(int(x)), str(int(y))])

    def type_text(self, text: str) -> None:
        # `input text` has no escaping of its own; spaces must be %s and the
        # rest is passed as a single argv element by the caller.
        self._run(["input", "text", text.replace(" ", "%s")])

    def scroll(self, direction: str, width: int, height: int) -> None:
        mid_x = width // 2
        near, far = int(height * 0.72), int(height * 0.28)
        start, end = (near, far) if direction == "down" else (far, near)
        self._run(["input", "swipe", str(mid_x), str(start), str(mid_x), str(end), "320"])

    def press(self, key: str) -> None:
        codes = {"enter": "66", "back": "4", "home": "3", "search": "84"}
        self._run(["input", "keyevent", codes.get(key, "66")])


class FakeBackend:
    """Scripted screens, for tests and for developing a workflow offline."""

    name = "fake"

    def __init__(self, screens: list[UiSnapshot] | None = None) -> None:
        self.screens = list(screens or [])
        self.calls: list[tuple[str, Any]] = []
        self.launched: list[str] = []
        self.urls: list[str] = []
        self.typed: list[str] = []
        self.unavailable = ""

    def available(self) -> tuple[bool, str]:
        return (False, self.unavailable) if self.unavailable else (True, "")

    def launch(self, package: str) -> None:
        self.launched.append(package)
        self.calls.append(("launch", package))

    def open_url(self, url: str) -> None:
        self.urls.append(url)
        self.calls.append(("url", url))

    def snapshot(self) -> UiSnapshot:
        self.calls.append(("snapshot", None))
        if not self.screens:
            raise AutomationError("No screen scripted.", code="unsupported_screen")
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", (x, y)))

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        self.calls.append(("type", text))

    def scroll(self, direction: str, width: int, height: int) -> None:
        self.calls.append(("scroll", direction))

    def press(self, key: str) -> None:
        self.calls.append(("press", key))


def screen(*labels: str, package: str = "com.example", **kinds) -> UiSnapshot:
    """Build a snapshot for tests: `screen("Search", "India")`."""
    editable = set(kinds.get("editable", ()))
    resource_ids = kinds.get("resource_ids", {})
    snapshot_id = UiSnapshot.new_id()
    elements = tuple(
        UiElement(
            element_id=f"{snapshot_id}.{index}",
            text=label,
            description="",
            resource_id=resource_ids.get(label, ""),
            class_name="android.widget.EditText" if label in editable else "android.widget.TextView",
            clickable=True,
            editable=label in editable,
            enabled=True,
            bounds=(0, index * 100, 1080, index * 100 + 90),
        )
        for index, label in enumerate(labels)
    )
    return UiSnapshot(snapshot_id, package, elements, 1080, 1920)


def strip_tracking(url: str) -> str:
    return re.sub(r"[?&](utm_[^&]+|fbclid|gclid)", "", url)
