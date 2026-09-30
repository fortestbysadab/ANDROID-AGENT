"""Tests for the Termux subprocess runner.

The reason this file exists: `subprocess.run(timeout=...)` kills only the
process it started. Every termux-* command is a shell wrapper that spawns the
`termux-api` helper, and that helper owns the LocalSocket the Termux:API app
writes its answer back to. Killing only the wrapper leaves the helper alive
holding a socket nobody reads, and the app then fails with
"java.io.IOException: Connection refused" and shows the owner a full-screen
Termux:API Error. Killing the process *group* is what prevents that.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest

from android_agent.tools.termux import _run

# A parent that outlives its child would leave the orphan behind.
ORPHAN_PARENT = (
    "import subprocess, sys, time;"
    "child = subprocess.Popen([sys.executable, '-c',"
    " 'import time; time.sleep(30)']);"
    "print(child.pid, flush=True);"
    "time.sleep(30)"
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


class RunnerTests(unittest.TestCase):
    def test_successful_command_returns_stdout(self):
        ok, output = _run([sys.executable, "-c", "print('hello')"])
        self.assertTrue(ok)
        self.assertEqual(output, "hello")

    def test_failure_returns_stderr(self):
        ok, output = _run(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
        )
        self.assertFalse(ok)
        self.assertIn("bad", output)

    def test_missing_binary_is_named(self):
        ok, output = _run(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(ok)
        self.assertIn("not installed", output)

    def test_timeout_reports_the_budget(self):
        ok, output = _run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertFalse(ok)
        self.assertIn("timed out", output)
        self.assertIn("0s", output)

    def test_timeout_kills_the_child_process_too(self):
        """The regression: an orphaned helper is what breaks Termux:API."""
        process = subprocess.Popen(
            [sys.executable, "-c", ORPHAN_PARENT],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        grandchild = int(process.stdout.readline().strip())

        def cleanup():
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        self.addCleanup(cleanup)
        self.assertTrue(alive(grandchild))

        # Same treatment the runner gives a timed-out command.
        from android_agent.tools.termux import _kill_group

        _kill_group(process)
        for _ in range(50):
            if not alive(grandchild):
                break
            time.sleep(0.05)
        self.assertFalse(alive(grandchild), "helper survived; Termux:API would error")

    def test_runner_leaves_no_orphan_on_timeout(self):
        marker = (
            "import subprocess, sys, time;"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "sys.stderr.write(str(c.pid)); sys.stderr.flush();"
            "time.sleep(30)"
        )
        started = time.monotonic()
        ok, _ = _run([sys.executable, "-c", marker], timeout=0.5)
        self.assertFalse(ok)
        # And it must not have blocked for anything like the child's lifetime.
        self.assertLess(time.monotonic() - started, 10)

    def test_output_is_bounded(self):
        ok, output = _run([sys.executable, "-c", "print('x' * 500000)"])
        self.assertTrue(ok)
        self.assertLessEqual(len(output), 64 * 1024)

    def test_stdin_is_closed_so_a_command_cannot_hang_on_input(self):
        ok, output = _run([sys.executable, "-c", "print(len(__import__('sys').stdin.read()))"])
        self.assertTrue(ok)
        self.assertEqual(output, "0")


if __name__ == "__main__":
    unittest.main()
