"""Scoped file tools and Telegram artifact hand-off."""

from __future__ import annotations

import os
from typing import Any, Mapping

from .base import Risk, ToolContext, ToolResult, ToolSpec

_AGENT_FILES = os.path.realpath(os.path.expanduser("~/telegram_agent_v2/files"))
_READ_ROOTS = tuple(
    os.path.realpath(os.path.expanduser(path))
    for path in ("/sdcard", "~/telegram_agent_v2/files")
)
_MAX_TEXT_BYTES = 64 * 1024
_MAX_SEND_BYTES = 45 * 1024 * 1024


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _resolve_read_path(raw: str) -> str:
    path = os.path.realpath(os.path.expanduser(raw))
    if not _inside(path, _READ_ROOTS):
        raise ValueError("path is outside the allowed Android storage and agent files directories")
    return path


def _resolve_create_path(name: str) -> str:
    # Creation is intentionally limited to one app-owned directory. A simple
    # relative name may contain folders, but canonicalization blocks escape.
    os.makedirs(_AGENT_FILES, exist_ok=True)
    path = os.path.realpath(os.path.join(_AGENT_FILES, name))
    if not _inside(path, (_AGENT_FILES,)):
        raise ValueError("file name escapes the agent files directory")
    return path


def _create_text_file(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    try:
        path = _resolve_create_path(arguments["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a" if arguments.get("append", False) else "w"
        with open(path, mode, encoding="utf-8", newline="") as handle:
            handle.write(arguments["content"])
    except (OSError, ValueError) as exc:
        return ToolResult.error(str(exc), code="file_write_failed")
    return ToolResult.ok(
        "Text file created." if mode == "w" else "Text appended to file.",
        {"path": path, "bytes": os.path.getsize(path)},
    )


def _read_text_file(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    try:
        path = _resolve_read_path(arguments["path"])
        if not os.path.isfile(path):
            return ToolResult.error("The path is not a regular file.", code="not_a_file")
        size = os.path.getsize(path)
        if size > _MAX_TEXT_BYTES:
            return ToolResult.error("Text file is too large to inspect; use get_file instead.", code="file_too_large")
        with open(path, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except (OSError, ValueError) as exc:
        return ToolResult.error(str(exc), code="file_read_failed")
    return ToolResult.ok("Text file read.", {"path": path, "content": content, "bytes": size})


def _get_file(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    del context
    try:
        path = _resolve_read_path(arguments["path"])
        if not os.path.isfile(path):
            return ToolResult.error("The path is not a regular file.", code="not_a_file")
        size = os.path.getsize(path)
        if size > _MAX_SEND_BYTES:
            return ToolResult.error("File exceeds the 45 MB Telegram transfer limit.", code="file_too_large")
    except (OSError, ValueError) as exc:
        return ToolResult.error(str(exc), code="file_read_failed")
    return ToolResult.ok(
        "File is ready to send.",
        {"artifact_path": path, "artifact_name": os.path.basename(path), "bytes": size},
    )


def file_tools() -> list[ToolSpec]:
    path_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Absolute or home-relative file path under /sdcard or the agent files directory.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    return [
        ToolSpec(
            name="create_text_file",
            description=(
                "Create or append to a UTF-8 text file in the private Android Agent files directory. "
                "Use this when the owner asks to create notes, lists, scripts as text, JSON, CSV, or another text file. "
                "The name is relative; this tool cannot overwrite arbitrary device paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string", "maxLength": 200000},
                    "append": {"type": "boolean"},
                },
                "required": ["name", "content"],
                "additionalProperties": False,
            },
            risk=Risk.DEVICE_MUTATION,
            handler=_create_text_file,
            timeout_seconds=5,
        ),
        ToolSpec(
            name="read_text_file",
            description=(
                "Read a small UTF-8 text file so its content can be summarized or used in the current task. "
                "It accepts paths only under Android shared storage or the agent files directory. "
                "For binary or large files use get_file instead."
            ),
            input_schema=path_schema,
            risk=Risk.SENSITIVE_READ,
            handler=_read_text_file,
            timeout_seconds=5,
            idempotent=True,
        ),
        ToolSpec(
            name="get_file",
            description=(
                "Send an existing file to the owner's Telegram chat as a document. "
                "Use this when the owner asks to fetch, download, attach, or send a specific device file. "
                "The file must be under Android shared storage or the agent files directory and at most 45 MB."
            ),
            input_schema=path_schema,
            risk=Risk.SENSITIVE_READ,
            handler=_get_file,
            timeout_seconds=5,
            idempotent=True,
        ),
    ]
