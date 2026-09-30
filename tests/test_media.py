"""Media filing: folder layout, timestamped names, and browsable storage."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from android_agent.tools import media
from android_agent.tools.media import (
    MEDIA_KINDS,
    PHOTO,
    RECORDING,
    SCREENSHOT,
    build_filename,
    describe_library,
    latest,
    media_root,
    new_media_path,
    storage_advice,
)

NAME_PATTERN = re.compile(
    r"^(photo|screenshot|recording)_\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2}(-\d+)?\.[a-z0-9]+$"
)


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"ANDROID_AGENT_MEDIA_DIR": self.directory.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(self.directory.name)


class LayoutTests(_Isolated):
    def test_folder_per_kind(self):
        self.assertEqual(new_media_path(PHOTO).parent.name, "photos")
        self.assertEqual(new_media_path(RECORDING).parent.name, "recordings")
        self.assertEqual(new_media_path(SCREENSHOT).parent.name, "screenshots")

    def test_directories_are_created(self):
        new_media_path(PHOTO)
        self.assertTrue((self.root / "photos").is_dir())

    def test_filename_format_matches_requested_layout(self):
        moment = datetime(2026, 9, 29, 19, 21, 48)
        self.assertEqual(
            build_filename(PHOTO, moment), "photo_29-09-2026_19-21-48.jpg"
        )
        self.assertEqual(
            build_filename(SCREENSHOT, moment), "screenshot_29-09-2026_19-21-48.png"
        )
        self.assertEqual(
            build_filename(RECORDING, moment), "recording_29-09-2026_19-21-48.m4a"
        )

    def test_every_kind_produces_a_valid_name(self):
        for kind in MEDIA_KINDS.values():
            self.assertRegex(new_media_path(kind).name, NAME_PATTERN)

    def test_same_second_captures_do_not_collide(self):
        moment = datetime(2026, 9, 29, 19, 21, 48)
        first = new_media_path(PHOTO, moment)
        first.write_bytes(b"a")
        second = new_media_path(PHOTO, moment)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "photo_29-09-2026_19-21-48-2.jpg")

    def test_unsafe_extension_rejected(self):
        with self.assertRaises(ValueError):
            build_filename(PHOTO, extension="../../etc/passwd")

    def test_paths_stay_inside_the_media_root(self):
        for kind in MEDIA_KINDS.values():
            path = new_media_path(kind).resolve()
            self.assertTrue(str(path).startswith(str(self.root.resolve())))


class LibraryTests(_Isolated):
    def test_empty_library_message(self):
        self.assertIn("No media captured yet", describe_library())

    def test_library_lists_captures(self):
        for kind in (PHOTO, SCREENSHOT):
            new_media_path(kind).write_bytes(b"x" * 100)
        summary = describe_library()
        self.assertIn("photos/", summary)
        self.assertIn("screenshots/", summary)

    def test_latest_returns_newest_first(self):
        older = new_media_path(PHOTO, datetime(2026, 9, 29, 10, 0, 0))
        older.write_bytes(b"a")
        os.utime(older, (1000, 1000))
        newer = new_media_path(PHOTO, datetime(2026, 9, 29, 11, 0, 0))
        newer.write_bytes(b"b")
        os.utime(newer, (2000, 2000))
        self.assertEqual(latest(PHOTO, 1), [newer])


class StorageLocationTests(unittest.TestCase):
    """Media must land somewhere the owner can actually open."""

    def test_explicit_override_wins(self):
        custom = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(custom, ignore_errors=True))
        with mock.patch.dict(os.environ, {"ANDROID_AGENT_MEDIA_DIR": custom}):
            self.assertEqual(media_root(), Path(custom))
            self.assertIsNone(storage_advice())

    def test_prefers_shared_storage_when_available(self):
        with mock.patch.dict(os.environ, {"ANDROID_AGENT_MEDIA_DIR": ""}), mock.patch.object(
            media, "shared_storage_base", return_value=Path("/sdcard")
        ):
            self.assertEqual(media_root(), Path("/sdcard/AndroidAgent"))
            self.assertIsNone(storage_advice())

    def test_falls_back_to_private_dir_and_advises(self):
        with mock.patch.dict(os.environ, {"ANDROID_AGENT_MEDIA_DIR": ""}), mock.patch.object(
            media, "shared_storage_base", return_value=None
        ):
            self.assertTrue(str(media_root()).endswith("telegram_agent_v2/media"))
            advice = storage_advice()
            self.assertIsNotNone(advice)
            self.assertIn("termux-setup-storage", advice)


if __name__ == "__main__":
    unittest.main()
