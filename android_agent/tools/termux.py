"""Initial narrow Termux tools for the v2 agent.

All subprocesses use argument arrays, bounded output, and explicit timeouts.
No generic shell capability is exposed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Mapping
from typing import Any

from .base import Risk, ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_OUTPUT = 64 * 1024


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the whole process group, not just the script we launched.

    A termux-* command is a shell wrapper that starts the `termux-api` helper
    and an `am broadcast`. Killing only the wrapper leaves the helper holding
    the LocalSocket that the Termux:API app writes its answer back to. When
    the app then tries to deliver, it hits "java.io.IOException: Connection
    refused" and throws a full-screen Termux:API Error at the user. Killing
    the group takes the helper with it, so there is no orphan left listening.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run(args: list[str], timeout: float = 12.0) -> tuple[bool, str]:
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Own process group, so a timeout can take down the helper too.
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"{args[0]} is not installed"
    except OSError as exc:
        return False, f"{args[0]} failed: {type(exc).__name__}"

    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        # Worth a warning, not a debug line: killing the client is also what
        # makes the Termux:API app fail to deliver and show the owner a
        # "Connection refused" error screen.
        logger.warning("%s timed out after %.0fs and was killed", " ".join(args), timeout)
        return False, f"{args[0]} timed out after {timeout:.0f}s"
    except OSError as exc:
        _kill_group(process)
        logger.warning("%s failed: %s", args[0], type(exc).__name__)
        return False, f"{args[0]} failed: {type(exc).__name__}"

    stdout = (raw_out or b"")[:_MAX_OUTPUT].decode("utf-8", "replace").strip()
    stderr = (raw_err or b"")[:2048].decode("utf-8", "replace").strip()
    if process.returncode != 0:
        reason = stderr or stdout or f"command exited {process.returncode}"
        logger.warning("%s exited %s: %s", args[0], process.returncode, reason[:200])
        return False, reason
    if not stdout:
        # Exit 0 with no output is a real Termux:API failure mode; without a
        # log line it is indistinguishable from a tool that returns nothing.
        logger.info("%s exited 0 with no output", " ".join(args))
    return True, stdout


def _battery(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _run(["termux-battery-status"])
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return ToolResult.error("Battery service returned invalid data.", code="invalid_device_data")
    data = {
        key: value.get(key)
        for key in ("percentage", "status", "health", "plugged", "temperature")
        if key in value
    }
    return ToolResult.ok("Battery status retrieved.", data)


def _set_torch(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    enabled = arguments["enabled"]
    ok, output = _run(["termux-torch", "on" if enabled else "off"])
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    return ToolResult.ok(f"Torch turned {'on' if enabled else 'off'}.", {"enabled": enabled})


def _set_brightness(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    level = arguments["level"]
    ok, output = _run(["termux-brightness", str(level)])
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    return ToolResult.ok("Brightness changed.", {"level": level})


def _set_volume(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    stream, level = arguments["stream"], arguments["level"]
    ok, output = _run(["termux-volume", stream, str(level)])
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    return ToolResult.ok("Volume changed.", {"stream": stream, "level": level})


def _get_volumes(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context, arguments
    ok, output = _run(["termux-volume"])
    if not ok:
        return ToolResult.error(output, code="termux_api_error", retryable=True)
    try:
        values = json.loads(output)
    except json.JSONDecodeError:
        return ToolResult.error("Volume service returned invalid data.", code="invalid_device_data")
    safe = [
        {"stream": item.get("stream"), "volume": item.get("volume"), "max_volume": item.get("max_volume")}
        for item in values
        if isinstance(item, dict)
    ]
    return ToolResult.ok("Volume levels retrieved.", {"streams": safe})


def build_termux_registry() -> ToolRegistry:
    no_args = {"type": "object", "properties": {}, "additionalProperties": False}
    return ToolRegistry(
        [
            ToolSpec(
                name="get_battery_status",
                description=(
                    "Read the Android device battery percentage, charging state, health, and temperature. "
                    "Use this only when the user asks about battery or charging status. "
                    "It does not change any device setting."
                ),
                input_schema=no_args,
                risk=Risk.READ_ONLY,
                handler=_battery,
                idempotent=True,
            ),
            ToolSpec(
                name="set_torch",
                description=(
                    "Turn the Android camera torch on or off. Use it only for an explicit request to control "
                    "the flashlight or torch. The enabled argument is true for on and false for off."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
                risk=Risk.REVERSIBLE,
                handler=_set_torch,
                idempotent=True,
            ),
            ToolSpec(
                name="set_brightness",
                description=(
                    "Set Android screen brightness to an exact raw level from 0 through 255. "
                    "Use only when the user states a numeric brightness value. "
                    "A level of 0 may make the screen appear off but does not lock it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 255}},
                    "required": ["level"],
                    "additionalProperties": False,
                },
                risk=Risk.REVERSIBLE,
                handler=_set_brightness,
                idempotent=True,
            ),
            ToolSpec(
                name="get_volume_levels",
                description=(
                    "Read current and maximum Android volume levels for every audio stream. "
                    "Use this when the user asks how loud the device or a stream currently is. "
                    "It does not change volume."
                ),
                input_schema=no_args,
                risk=Risk.READ_ONLY,
                handler=_get_volumes,
                idempotent=True,
            ),
            ToolSpec(
                name="set_volume",
                description=(
                    "Set one Android audio stream to an exact raw level. First use get_volume_levels when the "
                    "requested number may exceed that stream's device-specific maximum. The level is not a percentage."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "stream": {
                            "type": "string",
                            "enum": ["call", "system", "ring", "music", "alarm", "notification"],
                        },
                        "level": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "required": ["stream", "level"],
                    "additionalProperties": False,
                },
                risk=Risk.REVERSIBLE,
                handler=_set_volume,
                idempotent=True,
            ),
        ]
    )
