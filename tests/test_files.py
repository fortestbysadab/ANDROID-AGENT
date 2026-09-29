import unittest

from android_agent.tools.files import _resolve_create_path, _resolve_read_path


class FileBoundaryTests(unittest.TestCase):
    def test_create_path_cannot_escape_agent_directory(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            _resolve_create_path("../outside.txt")

    def test_read_path_rejects_system_files(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            _resolve_read_path("/etc/passwd")


if __name__ == "__main__":
    unittest.main()
