"""Additional narrow Termux:API tools migrated from the legacy bot."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from typing import Any

from .base import Risk, ToolContext, ToolResult, ToolSpec
from .files import _resolve_read_path
from .media import PHOTO, RECORDING, new_media_path
from .termux import _run

#: Path of the recording currently in progress, chosen when it starts so the
#: filename carries the time the recording began.
_recording_lock = threading.Lock()
_recording_path: str | None = None


def _json_command(command: list[str], summary: str, fields: tuple[str, ...] | None = None):
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context, arguments
        ok, output = _run(command)
        if not ok:
            return ToolResult.error(output, code="termux_api_error", retryable=True)
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return ToolResult.error("Termux API returned invalid data.", code="invalid_device_data")
        if fields and isinstance(data, dict):
            data = {key: data.get(key) for key in fields if key in data}
        if isinstance(data, list):
            data = data[:20]
            return ToolResult.ok(summary, {"items": data, "count_returned": len(data)})
        return ToolResult.ok(summary, data)
    return handler


def _command(builder, success, *, timeout=15):
    def handler(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        del context
        ok, output = _run(builder(arguments), timeout=timeout)
        if not ok:
            return ToolResult.error(output, code="termux_api_error", retryable=True)
        return ToolResult.ok(success(arguments), {"output": output} if output else {})
    return handler


def _camera(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    path = new_media_path(PHOTO)
    camera = "1" if arguments["camera"] == "front" else "0"
    ok, output = _run(["termux-camera-photo", "-c", camera, str(path)], timeout=25)
    if not ok or not path.exists() or path.stat().st_size == 0:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return ToolResult.error(
            output or "Camera produced no image.", code="camera_error", retryable=True
        )
    return ToolResult.ok(
        f"Photo captured and saved as {path.name}.",
        {
            "artifact_path": str(path),
            "artifact_name": path.name,
            "saved_to": str(path),
            "media_kind": "photo",
        },
    )


#: Loose international phone format. Digits, optional leading +, and the
#: separators people actually type. Rejects anything else outright.
PHONE_PATTERN = r"^\+?[0-9][0-9 ()\-\.]{2,24}$"


def _normalise_number(raw: str) -> str:
    """Strip formatting the dialer does not need, keeping a leading +."""
    cleaned = raw.strip()
    plus = cleaned.startswith("+")
    digits = "".join(character for character in cleaned if character.isdigit())
    return ("+" if plus else "") + digits


def _place_call(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    """Place a call, and be honest about whether it actually started.

    `termux-telephony-call` frequently exits 0 while doing nothing:

    * Termux:API lacks the CALL_PHONE runtime permission, or
    * Android's background-activity-launch restrictions block the dialer
      because Termux is not in the foreground.

    The old implementation reported success on exit code 0 alone, so the
    agent cheerfully claimed a call was placed when nothing happened. This
    checks what it can and otherwise reports an explicit "unconfirmed".
    """
    del context
    number = _normalise_number(str(arguments["number"]))
    if not number.lstrip("+"):
        return ToolResult.error("That is not a usable phone number.", code="invalid_number")

    ok, output = _run(["termux-telephony-call", number], timeout=20)
    lowered = output.lower()

    if not ok:
        if "permission" in lowered or "denied" in lowered:
            return ToolResult.error(
                "The call was refused: Termux:API does not have the Phone permission. "
                "Open Android Settings > Apps > Termux:API > Permissions and allow Phone, "
                "then try again.",
                code="call_permission_denied",
            )
        if "not installed" in lowered:
            return ToolResult.error(
                "termux-telephony-call is missing. Install it with `pkg install termux-api` "
                "and install the Termux:API app from F-Droid.",
                code="termux_api_missing",
            )
        if "timed out" in lowered:
            return ToolResult.error(
                "The dialer did not respond. This usually means Termux is in the background; "
                "Android blocks background apps from starting a call. Open Termux and retry.",
                code="call_timeout",
                retryable=True,
            )
        return ToolResult.error(
            output or "The call could not be placed.", code="call_failed", retryable=True
        )

    # Exit code 0 is not proof. Confirm the radio actually left idle state.
    state = _call_state()
    if state in {"OFFHOOK", "RINGING"}:
        return ToolResult.ok(
            f"Calling {number} now.",
            {"number": number, "call_state": state, "confirmed": True},
        )
    if state == "IDLE":
        return ToolResult.error(
            f"The dial request for {number} was accepted but no call started. "
            "Android blocks calls from background apps: bring Termux to the foreground, "
            "and check Termux:API has the Phone permission.",
            code="call_not_started",
            retryable=True,
        )
    return ToolResult.ok(
        f"Dial request sent for {number}. I could not confirm the call started; "
        "check your phone screen.",
        {"number": number, "confirmed": False},
    )


def _call_state() -> str | None:
    """Best-effort read of the telephony call state.

    Returns IDLE, RINGING, OFFHOOK, or None when it cannot be determined.
    """
    ok, output = _run(["termux-telephony-deviceinfo"], timeout=10)
    if not ok:
        return None
    try:
        info = json.loads(output)
    except json.JSONDecodeError:
        return None
    state = info.get("call_state") if isinstance(info, dict) else None
    return str(state).upper() if isinstance(state, str) else None


def _location(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    provider = arguments.get("provider", "network")
    ok, output = _run(["termux-location", "-p", provider, "-r", "once"], timeout=25)
    if not ok:
        return ToolResult.error(output, code="location_error", retryable=True)
    try:
        data = json.loads(output)
        result = {key: data.get(key) for key in ("latitude", "longitude", "accuracy", "altitude", "bearing") if key in data}
    except json.JSONDecodeError:
        return ToolResult.error("Location service returned invalid data.", code="invalid_device_data")
    return ToolResult.ok("Location retrieved.", result)


def _sysinfo(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    result = {}
    for name, command in (
        ("memory", ["free", "-h"]),
        ("storage", ["df", "-h", "/sdcard"]),
        ("uptime", ["uptime"]),
    ):
        ok, output = _run(command, timeout=10)
        result[name] = output if ok else f"unavailable: {output}"
    return ToolResult.ok("System information retrieved.", result)


def _clipboard_get(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _run(["termux-clipboard-get"])
    return ToolResult.ok("Clipboard read.", {"text": output}) if ok else ToolResult.error(output, code="termux_api_error")


def _audio_start(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    global _recording_path
    # Stop anything already running so the new file is clean.
    _run(["termux-microphone-record", "-q"], timeout=10)
    path = new_media_path(RECORDING)
    ok, output = _run(["termux-microphone-record", "-f", str(path)], timeout=10)
    if not ok:
        return ToolResult.error(output, code="recording_error", retryable=True)
    with _recording_lock:
        _recording_path = str(path)
    return ToolResult.ok(
        f"Microphone recording started, saving to {path.name}. "
        "Ask me to stop recording when finished.",
        {"saved_to": str(path), "media_kind": "recording"},
    )


def _audio_stop(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    global _recording_path
    with _recording_lock:
        path = _recording_path
        _recording_path = None

    ok, output = _run(["termux-microphone-record", "-q"], timeout=10)
    if not ok:
        return ToolResult.error(output, code="recording_error", retryable=True)
    if path is None:
        return ToolResult.error(
            "No recording was in progress. Start one first.", code="recording_missing"
        )
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return ToolResult.error("No non-empty recording was found.", code="recording_missing")
    return ToolResult.ok(
        f"Microphone recording stopped and saved as {os.path.basename(path)}.",
        {
            "artifact_path": path,
            "artifact_name": os.path.basename(path),
            "saved_to": path,
            "media_kind": "recording",
        },
    )


def _network_info(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, addresses = _run(["ip", "-4", "addr"], timeout=10)
    data: dict[str, Any] = {"ip_addresses": addresses if ok else "unavailable"}
    wifi_ok, wifi_output = _run(["termux-wifi-connectioninfo"], timeout=10)
    if wifi_ok:
        try:
            wifi = json.loads(wifi_output)
            data["wifi"] = {
                key: wifi.get(key)
                for key in ("ssid", "ip", "link_speed_mbps", "rssi")
                if key in wifi
            }
        except json.JSONDecodeError:
            pass
    return ToolResult.ok("Network information retrieved.", data)


def _set_wallpaper(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    try:
        path = _resolve_read_path(arguments["path"])
    except ValueError as exc:
        return ToolResult.error(str(exc), code="invalid_path")
    if not os.path.isfile(path):
        return ToolResult.error("Wallpaper path is not a regular file.", code="not_a_file")
    ok, output = _run(["termux-wallpaper", "-f", path], timeout=30)
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    return ToolResult.ok("Wallpaper updated.", {"path": path})


def _fingerprint(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _run(["termux-fingerprint"], timeout=30)
    if not ok:
        return ToolResult.error(output, code="fingerprint_error", retryable=True)
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return ToolResult.error("Fingerprint service returned invalid data.", code="invalid_device_data")
    status = str(data.get("auth_result", "UNKNOWN"))
    return ToolResult.ok("Fingerprint check completed.", {"authenticated": status == "AUTH_RESULT_SUCCESS", "status": status})


def _dialog(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    ok, output = _run(
        ["termux-dialog", "text", "-t", arguments["title"], "-i", arguments["prompt"]],
        timeout=65,
    )
    if not ok:
        return ToolResult.error(output, code="dialog_error", retryable=True)
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return ToolResult.error("Dialog returned invalid data.", code="invalid_device_data")
    return ToolResult.ok("Device dialog completed.", {"text": data.get("text", ""), "code": data.get("code")})


def _contacts(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    ok, output = _run(["termux-contact-list"], timeout=20)
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    try:
        contacts = json.loads(output)
    except json.JSONDecodeError:
        return ToolResult.error("Contact service returned invalid data.", code="invalid_device_data")
    query = arguments["query"].casefold()
    matches = [
        {"name": item.get("name"), "number": item.get("number")}
        for item in contacts
        if query in str(item.get("name", "")).casefold()
    ][:10]
    return ToolResult.ok("Contact search completed.", {"matches": matches, "count": len(matches)})


def extra_termux_tools() -> list[ToolSpec]:
    no_args = {"type": "object", "properties": {}, "additionalProperties": False}
    def text_arg(name, maximum=1000):
        return {"type": "object", "properties": {name: {"type": "string", "minLength": 1, "maxLength": maximum}}, "required": [name], "additionalProperties": False}
    return [
        ToolSpec("capture_photo", "Capture one photo with the Android camera and send it to Telegram. Use only when the owner explicitly asks to take a photo, selfie, or camera snapshot. Camera content is sensitive.", {"type": "object", "properties": {"camera": {"type": "string", "enum": ["front", "back"]}}, "required": ["camera"], "additionalProperties": False}, Risk.SENSITIVE_READ, _camera, timeout_seconds=30),
        ToolSpec("get_location", "Get the Android device's current coordinates once. Use only when the owner explicitly asks where the device is or requests its location. This returns sensitive location data.", {"type": "object", "properties": {"provider": {"type": "string", "enum": ["network", "gps"]}}, "additionalProperties": False}, Risk.SENSITIVE_READ, _location, timeout_seconds=30, idempotent=True),
        ToolSpec("get_system_info", "Read a snapshot of device memory, shared-storage usage, and uptime. Use for system health, RAM, storage, or uptime questions. This does not change the device.", no_args, Risk.READ_ONLY, _sysinfo, idempotent=True),
        ToolSpec("get_network_info", "Read local IPv4 interfaces and a concise current Wi-Fi network snapshot. Use when the owner asks about device IP addresses, network state, or connectivity. This does not contact a public IP service.", no_args, Risk.SENSITIVE_READ, _network_info, idempotent=True),
        ToolSpec("get_wifi_info", "Read details about the current Wi-Fi connection, including SSID, IP address, link speed, and signal strength. Use for questions about the active Wi-Fi network. This does not scan other networks.", no_args, Risk.SENSITIVE_READ, _json_command(["termux-wifi-connectioninfo"], "Wi-Fi information retrieved."), idempotent=True),
        ToolSpec("set_wifi", "Enable or disable Android Wi-Fi. Use only for an explicit request to turn Wi-Fi on or off. This changes network connectivity and may interrupt the agent.", {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False}, Risk.DEVICE_MUTATION, _command(lambda a: ["termux-wifi-enable", "true" if a["enabled"] else "false"], lambda a: "Wi-Fi enabled." if a["enabled"] else "Wi-Fi disabled."), idempotent=True),
        ToolSpec("get_clipboard", "Read the current Android clipboard text. Use only when the owner explicitly asks what is copied or asks to use clipboard content. Clipboard data may contain sensitive information.", no_args, Risk.SENSITIVE_READ, _clipboard_get, idempotent=True),
        ToolSpec("set_clipboard", "Replace Android clipboard text with exact owner-provided text. Use when the owner explicitly asks to copy or place text on the clipboard. Preserve the text exactly.", text_arg("text", 10000), Risk.DEVICE_MUTATION, _command(lambda a: ["termux-clipboard-set", a["text"]], lambda a: "Clipboard updated."), idempotent=True),
        ToolSpec("speak_text", "Speak exact text aloud through Android text-to-speech. Use only when the owner asks the device to say, announce, or read something aloud. Preserve the requested wording.", text_arg("text", 2000), Risk.REVERSIBLE, _command(lambda a: ["termux-tts-speak", a["text"]], lambda a: "Text spoken aloud.", timeout=30)),
        ToolSpec("show_toast", "Show a short temporary toast message on the Android screen. Use when the owner asks to display a brief local message. This does not send the message externally.", text_arg("text", 300), Risk.REVERSIBLE, _command(lambda a: ["termux-toast", a["text"]], lambda a: "Toast displayed.")),
        ToolSpec("vibrate_device", "Vibrate the Android device for a bounded duration in milliseconds. Use for explicit requests to vibrate or get the device's attention. Duration is limited to 30 seconds.", {"type": "object", "properties": {"duration_ms": {"type": "integer", "minimum": 50, "maximum": 30000}}, "required": ["duration_ms"], "additionalProperties": False}, Risk.REVERSIBLE, _command(lambda a: ["termux-vibrate", "-d", str(a["duration_ms"])], lambda a: f"Vibrated for {a['duration_ms']} ms.")),
        ToolSpec("set_wake_lock", "Acquire or release the Termux wake lock. Use when the owner asks to keep the agent awake or allow normal sleep. This affects Termux process sleep behavior, not screen power.", {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False}, Risk.DEVICE_MUTATION, _command(lambda a: ["termux-wake-lock"] if a["enabled"] else ["termux-wake-unlock"], lambda a: "Wake lock enabled." if a["enabled"] else "Wake lock released."), idempotent=True),
        ToolSpec("set_wallpaper", "Set Android wallpaper from an existing local image under shared storage or the agent files directory. Use only when the owner explicitly identifies the image path to apply. This tool does not download images from URLs.", {"type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["path"], "additionalProperties": False}, Risk.DEVICE_MUTATION, _set_wallpaper, timeout_seconds=35),
        ToolSpec("authenticate_fingerprint", "Open the Android fingerprint authentication prompt and report whether the local touch succeeded. Use only when the owner asks to test or request fingerprint authentication. Someone physically at the device must touch the sensor.", no_args, Risk.SENSITIVE_READ, _fingerprint, timeout_seconds=35),
        ToolSpec("show_text_dialog", "Show a text-input dialog on the Android device and wait for the local user's response. Use when the owner asks to prompt someone at the device for information. The returned text is sensitive and the wait is limited.", {"type": "object", "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 80}, "prompt": {"type": "string", "minLength": 1, "maxLength": 300}}, "required": ["title", "prompt"], "additionalProperties": False}, Risk.DEVICE_MUTATION, _dialog, timeout_seconds=70),
        ToolSpec("control_media", "Control the current Android media player with play, pause, next, or previous. Use only for an explicit playback-control request. This does not select a specific media app.", {"type": "object", "properties": {"action": {"type": "string", "enum": ["play", "pause", "next", "previous"]}}, "required": ["action"], "additionalProperties": False}, Risk.REVERSIBLE, _command(lambda a: ["termux-media-player", a["action"]], lambda a: f"Media action {a['action']} sent.")),
        ToolSpec("start_audio_recording", "Start microphone recording on the Android device. Use only when the owner explicitly asks to begin an audio or microphone recording. Recording continues until stop_audio_recording is called.", no_args, Risk.SENSITIVE_READ, _audio_start),
        ToolSpec("stop_audio_recording", "Stop the active microphone recording and send the audio file to Telegram. Use when the owner asks to stop, finish, or retrieve the current recording. It fails safely when no recording exists.", no_args, Risk.SENSITIVE_READ, _audio_stop),
        ToolSpec("search_contacts", "Search Android contacts by a partial person or contact name. Use only when the owner asks to find contact details. Results contain sensitive phone numbers and are limited to ten matches.", text_arg("query", 100), Risk.SENSITIVE_READ, _contacts, idempotent=True),
        ToolSpec("get_recent_sms", "Read up to five recent SMS inbox messages. Use only when the owner explicitly asks to inspect recent texts or the SMS inbox. Message sender and body are sensitive data.", no_args, Risk.SENSITIVE_READ, _json_command(["termux-sms-list", "-l", "5"], "Recent SMS messages retrieved."), idempotent=True),
        ToolSpec("get_notifications", "Read up to the currently active Android notifications. Use only when the owner asks to check notifications. Notification titles and content may contain sensitive data.", no_args, Risk.SENSITIVE_READ, _json_command(["termux-notification-list"], "Active notifications retrieved."), idempotent=True),
        ToolSpec("place_phone_call", "Place a phone call to an exact number supplied by the owner. Use only when the owner explicitly asks to call that number. This external side effect requires confirmation before execution.", {"type": "object", "properties": {"number": {"type": "string", "minLength": 3, "maxLength": 25, "pattern": PHONE_PATTERN}}, "required": ["number"], "additionalProperties": False}, Risk.EXTERNAL_SIDE_EFFECT, _place_call, timeout_seconds=35.0),
        ToolSpec("send_sms", "Send an SMS to one exact phone number with exact message text. Use only when the owner explicitly asks to send the message, preserving destination and content. This external side effect requires confirmation before execution.", {"type": "object", "properties": {"number": {"type": "string", "minLength": 3, "maxLength": 25, "pattern": PHONE_PATTERN}, "message": {"type": "string", "minLength": 1, "maxLength": 1600}}, "required": ["number", "message"], "additionalProperties": False}, Risk.EXTERNAL_SIDE_EFFECT, _command(lambda a: ["termux-sms-send", "-n", a["number"], a["message"]], lambda a: f"SMS sent to {a['number']}.")),
    ]
