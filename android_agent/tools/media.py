"""Organised, timestamped storage for captured media.

Captured files are kept, not deleted after delivery, and filed by kind:

    <media root>/
      photos/      photo_29-09-2026_19-21-48.jpg
      recordings/  recording_29-09-2026_19-22-12.m4a
      screenshots/ screenshot_29-09-2026_19-24-25.png

The timestamp is local device time in `DD-MM-YYYY_HH-MM-SS`, which sorts
readably in a chat and is unambiguous on a phone file manager.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: Filename stamp format. Local time: these are user-facing artefacts.
TIMESTAMP_FORMAT = "%d-%m-%Y_%H-%M-%S"

#: Guards against a traversal or odd extension reaching the filesystem.
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,5}$")


@dataclass(frozen=True)
class MediaKind:
    name: str
    folder: str
    extension: str


PHOTO = MediaKind("photo", "photos", "jpg")
SCREENSHOT = MediaKind("screenshot", "screenshots", "png")
RECORDING = MediaKind("recording", "recordings", "m4a")

MEDIA_KINDS: dict[str, MediaKind] = {
    kind.name: kind for kind in (PHOTO, SCREENSHOT, RECORDING)
}

#: Shared storage, visible to the Gallery, Files app, USB/MTP and any other
#: app. `termux-setup-storage` creates ~/storage/shared as a symlink to
#: /sdcard. This is the only location the owner can actually browse.
SHARED_STORAGE_CANDIDATES = (
    "~/storage/shared",
    "/sdcard",
    "/storage/emulated/0",
)

#: Folder created inside shared storage.
SHARED_FOLDER_NAME = "AndroidAgent"

#: Fallback when storage has not been set up. Functional but only reachable
#: from inside Termux.
PRIVATE_MEDIA_DIR = "~/telegram_agent_v2/media"


def _writable(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False


def shared_storage_base() -> Path | None:
    """First writable shared-storage root, or None if unavailable."""
    for candidate in SHARED_STORAGE_CANDIDATES:
        resolved = Path(os.path.expanduser(candidate))
        if _writable(resolved):
            return resolved
    return None


def media_root() -> Path:
    """Where captured media is stored.

    Order of preference:

    1. `ANDROID_AGENT_MEDIA_DIR` when set, for full control.
    2. Shared storage (`/sdcard/AndroidAgent`), so files show up in the
       Gallery and any file manager.
    3. Termux's private home, which works but is not browsable outside
       Termux. Run `termux-setup-storage` to get option 2.
    """
    configured = os.environ.get("ANDROID_AGENT_MEDIA_DIR", "").strip()
    if configured:
        return Path(os.path.expanduser(configured))

    base = shared_storage_base()
    if base is not None:
        return base / SHARED_FOLDER_NAME
    return Path(os.path.expanduser(PRIVATE_MEDIA_DIR))


def storage_advice() -> str | None:
    """A hint to show when media is not in browsable storage."""
    if os.environ.get("ANDROID_AGENT_MEDIA_DIR", "").strip():
        return None
    if shared_storage_base() is not None:
        return None
    return (
        "These files are inside Termux's private folder, so your Gallery and "
        "file manager cannot see them. Run `termux-setup-storage` in Termux, "
        "grant the permission, then restart the bot to save media to "
        f"/sdcard/{SHARED_FOLDER_NAME} instead."
    )


def media_dir(kind: MediaKind) -> Path:
    directory = media_root() / kind.folder
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_filename(
    kind: MediaKind, moment: datetime | None = None, extension: str | None = None
) -> str:
    suffix = (extension or kind.extension).lower().lstrip(".")
    if not _SAFE_EXTENSION.match(suffix):
        raise ValueError(f"unsafe media extension: {extension!r}")
    stamp = (moment or datetime.now()).strftime(TIMESTAMP_FORMAT)
    return f"{kind.name}_{stamp}.{suffix}"


def new_media_path(
    kind: MediaKind, moment: datetime | None = None, extension: str | None = None
) -> Path:
    """Reserve a unique, timestamped path inside the right folder.

    Two captures within the same second get `-2`, `-3` suffixes rather than
    silently overwriting each other.
    """
    directory = media_dir(kind)
    base = build_filename(kind, moment, extension)
    candidate = directory / base

    if not candidate.exists():
        return candidate

    stem, _, suffix = base.rpartition(".")
    for counter in range(2, 1000):
        candidate = directory / f"{stem}-{counter}.{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("could not allocate a unique media filename")


def describe_library() -> str:
    """A short human summary of what has been captured so far."""
    root = media_root()
    if not root.is_dir():
        return "No media captured yet."

    lines: list[str] = [f"Media folder: {root}"]
    advice = storage_advice()
    if advice:
        lines.append(f"\nNote: {advice}")
    total = 0
    for kind in MEDIA_KINDS.values():
        directory = root / kind.folder
        if not directory.is_dir():
            continue
        files = sorted(
            (item for item in directory.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not files:
            continue
        total += len(files)
        size_mb = sum(item.stat().st_size for item in files) / (1024 * 1024)
        lines.append(f"\n{kind.folder}/  ({len(files)} files, {size_mb:.1f} MB)")
        for item in files[:5]:
            lines.append(f"  • {item.name}")
        if len(files) > 5:
            lines.append(f"  … {len(files) - 5} more")

    if total == 0:
        return f"No media captured yet.\nMedia folder: {root}"
    return "\n".join(lines)


def latest(kind: MediaKind, count: int = 1) -> list[Path]:
    """Most recently captured files of one kind, newest first."""
    directory = media_root() / kind.folder
    if not directory.is_dir():
        return []
    files = sorted(
        (item for item in directory.iterdir() if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return files[:count]
