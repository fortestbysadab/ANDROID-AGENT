"""Narrow wireless-ADB tools. No generic adb-shell tool is exposed."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Any

from .base import Risk, ToolContext, ToolResult, ToolSpec


def _run(args: list[str], timeout: float = 15) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, "adb is not installed"
    except subprocess.TimeoutExpired:
        return False, "adb timed out"
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()
        return False, error[:2000] or f"adb exited {result.returncode}"
    return True, result.stdout.decode("utf-8", "replace").strip()[:65536]


def _connected() -> bool:
    ok, output = _run(["adb", "devices"], 10)
    return ok and any(line.strip().endswith("\tdevice") for line in output.splitlines()[1:])


def _shell(arguments: list[str], timeout: float = 15) -> tuple[bool, str]:
    if not _connected():
        return False, "Wireless ADB is not connected; reconnect it in Termux and retry."
    return _run(["adb", "shell", *arguments], timeout)


def _simple_shell(args_builder, success_builder):
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context
        ok, output = _shell(args_builder(arguments))
        if not ok:
            return ToolResult.error(output, code="adb_error", retryable=True)
        return ToolResult.ok(success_builder(arguments))
    return handler


def _adb_status(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    connected = _connected()
    return ToolResult.ok(
        "Wireless ADB is connected." if connected else "Wireless ADB is not connected.",
        {"connected": connected},
    )


def _current_app(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _shell(["dumpsys", "window"])
    if not ok:
        return ToolResult.error(output, code="adb_error", retryable=True)
    match = re.search(r"mCurrentFocus=.*?\s([a-zA-Z0-9_.]+)/", output)
    if not match:
        match = re.search(r"mFocusedApp=.*?\s([a-zA-Z0-9_.]+)/", output)
    if not match:
        return ToolResult.error("Could not identify the foreground app.", code="device_data_missing")
    return ToolResult.ok("Foreground app identified.", {"package": match.group(1)})


def _set_screen_power(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    requested = arguments["on"]
    ok, output = _shell(["dumpsys", "power"])
    if not ok:
        return ToolResult.error(output, code="adb_error", retryable=True)
    match = re.search(r"mWakefulness=(Awake|Asleep|Dozing)", output, re.IGNORECASE)
    is_on = match is not None and match.group(1).casefold() == "awake"
    if match is not None and is_on == requested:
        return ToolResult.ok("Screen is already " + ("on." if requested else "off."), {"on": requested})
    ok, output = _shell(["input", "keyevent", "26"])
    if not ok:
        return ToolResult.error(output, code="adb_error", retryable=True)
    return ToolResult.ok("Screen turned " + ("on." if requested else "off."), {"on": requested})


def _list_apps(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _shell(["pm", "list", "packages", "-3"])
    if not ok:
        return ToolResult.error(output, code="adb_error", retryable=True)
    packages = sorted(line.removeprefix("package:").strip() for line in output.splitlines() if line.strip())
    return ToolResult.ok("Third-party apps listed.", {"count": len(packages), "packages": packages[:200]})


def _screenshot(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    if not _connected():
        return ToolResult.error("Wireless ADB is not connected.", code="adb_not_connected", retryable=True)
    directory = os.path.expanduser("~/telegram_agent_v2/artifacts")
    os.makedirs(directory, exist_ok=True)
    fd, local_path = tempfile.mkstemp(prefix="screenshot-", suffix=".png", dir=directory)
    os.close(fd)
    device_path = "/sdcard/.android_agent_screenshot.png"
    ok, output = _run(["adb", "shell", "screencap", "-p", device_path], 15)
    if ok:
        ok, output = _run(["adb", "pull", device_path, local_path], 20)
    _run(["adb", "shell", "rm", "-f", device_path], 10)
    if not ok or not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
        try:
            os.remove(local_path)
        except OSError:
            pass
        return ToolResult.error(output or "Screenshot failed.", code="adb_error", retryable=True)
    return ToolResult.ok(
        "Screenshot captured.",
        {"artifact_path": local_path, "artifact_name": "screenshot.png", "temporary_artifact": True},
    )


def adb_tools() -> list[ToolSpec]:
    no_args = {"type": "object", "properties": {}, "additionalProperties": False}
    def integer(minimum, maximum):
        return {"type": "integer", "minimum": minimum, "maximum": maximum}
    key_names = ["home", "back", "recents", "power", "volume_up", "volume_down", "enter", "delete", "play", "pause"]
    key_codes = {"home": 3, "back": 4, "recents": 187, "power": 26, "volume_up": 24, "volume_down": 25, "enter": 66, "delete": 67, "play": 126, "pause": 127}
    return [
        ToolSpec("get_adb_status", "Check whether same-device wireless ADB is connected. Use before explaining why an ADB-only action failed. This performs no device action.", no_args, Risk.READ_ONLY, _adb_status, idempotent=True),
        ToolSpec("capture_screenshot", "Capture the current Android screen and send the resulting PNG to Telegram. Use only when the owner explicitly asks for a screenshot or to see the current screen. This is a sensitive read of visible screen content.", no_args, Risk.SENSITIVE_READ, _screenshot),
        ToolSpec("get_current_app", "Identify the package name of the app currently in the foreground. Use when the owner asks which app is open or active. Wireless ADB must be connected.", no_args, Risk.READ_ONLY, _current_app, idempotent=True),
        ToolSpec("list_installed_apps", "List package names for third-party Android applications. Use when the owner asks what apps are installed or needs a package name. Wireless ADB must be connected.", no_args, Risk.READ_ONLY, _list_apps, idempotent=True),
        ToolSpec(
            "set_screen_power",
            "Set the Android display power on or off after checking its current wake state. Use only for an explicit screen-on or screen-off request. Wireless ADB must be connected.",
            {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"], "additionalProperties": False},
            Risk.DEVICE_MUTATION,
            _set_screen_power,
        ),
        ToolSpec(
            "tap_screen",
            "Tap one exact screen coordinate. Use only when the owner gives or clearly requests a specific coordinate as part of bounded UI control. Coordinates are pixels and wireless ADB must be connected.",
            {"type": "object", "properties": {"x": integer(0, 10000), "y": integer(0, 10000)}, "required": ["x", "y"], "additionalProperties": False},
            Risk.RAW_CONTROL,
            _simple_shell(lambda a: ["input", "tap", str(a["x"]), str(a["y"])], lambda a: f"Tapped ({a['x']}, {a['y']})."),
        ),
        ToolSpec(
            "swipe_screen",
            "Perform one bounded swipe between exact screen coordinates. Use only for an explicit swipe or scroll request with enough direction or coordinates to derive the gesture. Wireless ADB must be connected.",
            {"type": "object", "properties": {"x1": integer(0, 10000), "y1": integer(0, 10000), "x2": integer(0, 10000), "y2": integer(0, 10000), "duration_ms": integer(50, 5000)}, "required": ["x1", "y1", "x2", "y2", "duration_ms"], "additionalProperties": False},
            Risk.RAW_CONTROL,
            _simple_shell(lambda a: ["input", "swipe", str(a["x1"]), str(a["y1"]), str(a["x2"]), str(a["y2"]), str(a["duration_ms"])], lambda a: "Swipe completed."),
        ),
        ToolSpec(
            "send_key_event",
            "Send one named Android navigation or media key. Use for explicit requests such as go home, go back, show recents, press enter, or change volume with a key. Wireless ADB must be connected.",
            {"type": "object", "properties": {"key": {"type": "string", "enum": key_names}}, "required": ["key"], "additionalProperties": False},
            Risk.RAW_CONTROL,
            _simple_shell(lambda a: ["input", "keyevent", str(key_codes[a["key"]])], lambda a: f"Sent {a['key']} key."),
        ),
        ToolSpec(
            "type_text",
            "Type plain text into the currently focused Android input field. Use only when the owner explicitly asks to type exact text and preserve their wording. Wireless ADB must be connected; this cannot type every Unicode character reliably.",
            {"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["text"], "additionalProperties": False},
            Risk.RAW_CONTROL,
            _simple_shell(lambda a: ["input", "text", shlex.quote(a["text"].replace(" ", "%s"))], lambda a: "Text typed into the focused field."),
        ),
        ToolSpec(
            "force_stop_app",
            "Force-stop one Android app by exact package name. Use only when the owner asks to close, stop, or kill a named app and a package name is known. Use list_installed_apps first if the package is uncertain.",
            {"type": "object", "properties": {"package": {"type": "string", "pattern": "[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+", "maxLength": 200}}, "required": ["package"], "additionalProperties": False},
            Risk.DEVICE_MUTATION,
            _simple_shell(lambda a: ["am", "force-stop", a["package"]], lambda a: f"Force-stopped {a['package']}."),
        ),
    ]
