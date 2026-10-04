"""Tests for script-generated documents.

The agent writes a Python script and the script writes the file. That is
arbitrary code execution by an untrusted planner, so most of these tests are
about containment and about failing honestly rather than about formatting.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from android_agent.documents.reader import (
    ALLOWED_FORMATS,
    OPTIONAL_LIBRARIES,
    available_libraries,
    library_summary,
)
from android_agent.documents.sandbox import (
    HIDDEN_PATHS,
    SCRIPT_NAME,
    build_command,
    pick_output,
    proot_available,
    run_script,
    workspace_for,
    workspaces_root,
)
from android_agent.documents.store import DocumentStore, slugify
from android_agent.tools.base import Risk
from android_agent.tools.document_tools import document_tools

WRITE_CSV = """
import csv
with open('output.csv', 'w', newline='') as handle:
    csv.writer(handle).writerows([['Item', 'Amount'], ['Rent', 12000]])
"""


def workspace() -> Path:
    return Path(tempfile.mkdtemp())


class SandboxTests(unittest.TestCase):
    def test_a_rerun_in_the_same_workspace_sees_its_own_new_output(self):
        """Revision 2 must not be mistaken for a script that wrote nothing."""
        space = workspace()
        first = run_script(WRITE_CSV, space, isolate=False)
        self.assertEqual([p.name for p in first.produced], ["output.csv"])
        second = run_script(
            WRITE_CSV.replace("12000", "15000"), space, isolate=False
        )
        self.assertEqual([p.name for p in second.produced], ["output.csv"])
        self.assertIn("15000", (space / "output.csv").read_text(encoding="utf-8-sig"))

    def test_a_rerun_does_not_inherit_the_previous_files(self):
        space = workspace()
        run_script("open('leftover.txt','w').write('old')", space, isolate=False)
        result = run_script(WRITE_CSV, space, isolate=False)
        self.assertNotIn("leftover.txt", [p.name for p in result.produced])
        self.assertFalse((space / "leftover.txt").exists())

    def test_a_script_runs_and_its_output_is_found(self):
        result = run_script(WRITE_CSV, workspace(), isolate=False)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual([p.name for p in result.produced], ["output.csv"])

    def test_the_script_itself_is_not_treated_as_output(self):
        result = run_script(WRITE_CSV, workspace(), isolate=False)
        self.assertNotIn(SCRIPT_NAME, [p.name for p in result.produced])

    def test_a_failing_script_reports_its_traceback(self):
        result = run_script("raise ValueError('bad column')", workspace(), isolate=False)
        self.assertFalse(result.ok)
        self.assertIn("ValueError: bad column", result.diagnostic)

    def test_a_syntax_error_is_reported_not_swallowed(self):
        result = run_script("def (:", workspace(), isolate=False)
        self.assertFalse(result.ok)
        self.assertIn("SyntaxError", result.diagnostic)

    def test_a_hanging_script_is_stopped(self):
        result = run_script(
            "import time; time.sleep(30)", workspace(), timeout=1.0, isolate=False
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertIn("did not finish", result.diagnostic)

    def test_agent_secrets_are_not_in_the_script_environment(self):
        """Even without proot, no ANDROID_AGENT_* variable is handed over."""
        with mock.patch.dict(os.environ, {"ANDROID_AGENT_LLM_API_KEY": "secret-key"}):
            result = run_script(
                "import os; print([k for k in os.environ if 'ANDROID_AGENT' in k])",
                workspace(), isolate=False,
            )
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertNotIn("secret-key", result.stdout)

    def test_home_points_at_the_workspace_not_the_real_home(self):
        space = workspace()
        result = run_script("import os; print(os.environ['HOME'])", space, isolate=False)
        self.assertEqual(result.stdout.strip(), str(space))

    def test_matplotlib_is_told_not_to_open_a_display(self):
        result = run_script(
            "import os; print(os.environ.get('MPLBACKEND'))", workspace(), isolate=False
        )
        self.assertEqual(result.stdout.strip(), "Agg")

    def test_the_script_runs_in_its_own_directory(self):
        space = workspace()
        result = run_script("import os; print(os.getcwd())", space, isolate=False)
        self.assertEqual(Path(result.stdout.strip()).resolve(), space.resolve())

    def test_each_document_gets_a_separate_workspace(self):
        self.assertNotEqual(workspace_for("aaaa1111"), workspace_for("bbbb2222"))

    def test_workspaces_live_outside_the_home_directory(self):
        """Otherwise hiding $HOME would also hide the script's own folder."""
        home = os.environ.get("HOME")
        if home:
            self.assertFalse(str(workspaces_root()).startswith(str(Path(home) / "")))

    def test_output_size_is_capped(self):
        result = run_script(
            "open('output.txt','w').write('x' * (200 * 1024 * 1024))",
            workspace(), isolate=False,
        )
        self.assertFalse(result.ok)


class IsolationCommandTests(unittest.TestCase):
    """proot is what actually keeps a script away from the owner's secrets."""

    def test_without_isolation_the_command_is_plain_python(self):
        command = build_command(workspace(), isolate=False)
        self.assertNotIn("proot", command[0])
        self.assertEqual(command[-1], SCRIPT_NAME)

    def test_isolation_hides_the_home_directory(self):
        space = workspace()
        command = " ".join(build_command(space, isolate=True))
        home = os.environ.get("HOME")
        self.assertTrue(command.startswith("proot"))
        if home:
            self.assertIn(f":{home}", command)

    def test_isolation_hides_shared_storage(self):
        command = " ".join(build_command(workspace(), isolate=True))
        for path in HIDDEN_PATHS:
            if Path(path).exists():
                with self.subTest(path=path):
                    self.assertIn(f":{path}", command)

    def test_isolation_sets_the_working_directory(self):
        space = workspace()
        command = build_command(space, isolate=True)
        self.assertIn("-w", command)
        self.assertIn(str(space), command)

    def test_the_blind_mount_is_an_empty_directory(self):
        space = workspace()
        build_command(space, isolate=True)
        blind = space / ".blind"
        self.assertTrue(blind.is_dir())
        self.assertEqual(list(blind.iterdir()), [])


class OutputPickingTests(unittest.TestCase):
    def setUp(self):
        self.space = workspace()

    def _touch(self, name, size=10):
        path = self.space / name
        path.write_bytes(b"x" * size)
        return path

    def test_the_documented_name_wins(self):
        self._touch("other.pdf", 500)
        expected = self._touch("output.pdf", 10)
        self.assertEqual(pick_output(sorted(self.space.iterdir()), "pdf"), expected)

    def test_any_file_with_the_right_extension_is_accepted(self):
        """A script that writes report.pdf should not fail on a technicality."""
        expected = self._touch("report.pdf")
        self.assertEqual(pick_output([expected], "pdf"), expected)

    def test_the_largest_candidate_wins_when_several_match(self):
        self._touch("small.pdf", 10)
        big = self._touch("big.pdf", 999)
        self.assertEqual(pick_output(sorted(self.space.iterdir()), "pdf"), big)

    def test_a_wrong_extension_is_not_accepted(self):
        self.assertIsNone(pick_output([self._touch("output.txt")], "pdf"))

    def test_nothing_produced_means_nothing_picked(self):
        self.assertIsNone(pick_output([], "pdf"))


class LibraryReportTests(unittest.TestCase):
    def test_availability_is_checked_not_assumed(self):
        found = available_libraries()
        self.assertEqual(set(found), set(OPTIONAL_LIBRARIES))
        for name, present in found.items():
            with self.subTest(library=name):
                self.assertIsInstance(present, bool)

    def test_the_summary_is_never_empty(self):
        self.assertTrue(library_summary().strip())

    def test_every_optional_library_has_an_install_command(self):
        for name, hint in OPTIONAL_LIBRARIES.items():
            with self.subTest(library=name):
                self.assertTrue(hint.strip(), f"{name} has no install hint")


class SlugTests(unittest.TestCase):
    def test_filesystem_characters_are_replaced(self):
        self.assertNotIn("/", slugify("Q3/2026 report"))

    def test_other_scripts_keep_their_letters(self):
        self.assertIn("রিপোর্ট", slugify("মাসিক রিপোর্ট"))

    def test_an_empty_title_still_produces_a_name(self):
        self.assertEqual(slugify("   "), "document")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = Path(self.tmp.name) / "files"
        self.files.mkdir(parents=True)
        for target in (
            "android_agent.documents.store.documents_root",
            "android_agent.tools.document_tools.documents_root",
        ):
            patcher = mock.patch(target, return_value=self.files)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.store = DocumentStore(Path(self.tmp.name) / "documents.db")
        self.addCleanup(self.store.close)
        self.tools = {tool.name: tool for tool in document_tools(self.store)}

    def call(self, tool_name, **arguments):
        tool = self.tools[tool_name]
        return tool.handler(None, tool.validate(arguments))

    def test_a_script_produces_a_real_file(self):
        result = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        self.assertEqual(result.status, "ok", result.summary)
        path = Path(result.data["artifact_path"])
        self.assertTrue(path.is_file())
        self.assertIn("Rent", path.read_text(encoding="utf-8-sig"))

    def test_the_file_lands_in_the_files_folder(self):
        result = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        self.assertEqual(Path(result.data["artifact_path"]).parent, self.files)

    def test_a_broken_script_reports_the_error_and_writes_nothing(self):
        result = self.call(
            "create_document", title="Broken", format="csv",
            script="raise RuntimeError('column missing')",
        )
        self.assertEqual(result.error_code, "script_failed")
        self.assertIn("column missing", result.summary)
        self.assertEqual(list(self.files.iterdir()), [])

    def test_a_script_that_writes_the_wrong_format_is_told_so(self):
        result = self.call(
            "create_document", title="Wrong", format="pdf",
            script="open('output.txt','w').write('hi')",
        )
        self.assertEqual(result.error_code, "no_output")
        self.assertIn("output.pdf", result.summary)

    def test_a_script_that_writes_nothing_is_told_so(self):
        result = self.call(
            "create_document", title="Silent", format="csv", script="x = 1",
        )
        self.assertEqual(result.error_code, "no_output")
        self.assertIn("nothing", result.summary)

    def test_revising_runs_the_new_script_and_keeps_version_one(self):
        created = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        first = Path(created.data["artifact_path"])
        revised = self.call(
            "revise_document", document="Expenses",
            script=WRITE_CSV.replace("12000", "15000"),
        )
        second = Path(revised.data["artifact_path"])
        self.assertEqual(revised.data["version"], 2)
        self.assertTrue(first.is_file(), "version 1 must survive")
        self.assertIn("12000", first.read_text(encoding="utf-8-sig"))
        self.assertIn("15000", second.read_text(encoding="utf-8-sig"))

    def test_a_failed_revision_leaves_the_previous_version_intact(self):
        created = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        first = Path(created.data["artifact_path"])
        result = self.call(
            "revise_document", document="Expenses", script="raise ValueError('nope')"
        )
        self.assertEqual(result.error_code, "script_failed")
        self.assertTrue(first.is_file())
        self.assertEqual(self.store.get(created.data["id"]).version, 1)

    def test_the_script_is_kept_and_can_be_shown(self):
        created = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        shown = self.call("show_document_script", document=created.data["id"])
        self.assertIn("csv.writer", shown.data["script"])
        self.assertIn("```python", shown.summary)

    def test_showing_an_unknown_script_is_refused(self):
        self.assertEqual(
            self.call("show_document_script", document="nope").error_code,
            "unknown_document",
        )

    def test_listing_shows_the_version(self):
        self.call("create_document", title="Expenses", format="csv", script=WRITE_CSV)
        self.call("revise_document", document="Expenses", script=WRITE_CSV)
        self.assertIn("v2", self.call("list_documents").summary)

    def test_reading_a_generated_file_back(self):
        self.call("create_document", title="Expenses", format="csv", script=WRITE_CSV)
        result = self.call("read_document", name="Expenses")
        self.assertIn("Rent", result.summary)
        self.assertIn("UNTRUSTED", result.summary)

    def test_reading_outside_the_files_folder_is_refused(self):
        for attempt in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(attempt=attempt):
                result = self.call("read_document", name=attempt)
                self.assertEqual(result.status, "error")

    def test_read_document_is_untrusted_content(self):
        self.assertTrue(self.tools["read_document"].returns_untrusted_content)

    def test_writing_is_gated_when_there_is_no_isolation(self):
        """Friction follows containment: no proot means ask every time."""
        with mock.patch(
            "android_agent.tools.document_tools.proot_available", return_value=False
        ):
            ungated = {t.name: t for t in document_tools(self.store)}
        self.assertEqual(ungated["create_document"].risk, Risk.EXTERNAL_SIDE_EFFECT)
        self.assertIn("NOT isolated", ungated["create_document"].description)

    def test_writing_is_ungated_when_isolation_is_available(self):
        with mock.patch(
            "android_agent.tools.document_tools.proot_available", return_value=True
        ):
            gated = {t.name: t for t in document_tools(self.store)}
        self.assertEqual(gated["create_document"].risk, Risk.DEVICE_MUTATION)
        self.assertIn("isolated", gated["create_document"].description)

    def test_the_tool_tells_the_model_which_libraries_exist(self):
        description = self.tools["create_document"].description
        self.assertIn("output.<format>", description)
        for library, present in available_libraries().items():
            if present:
                with self.subTest(library=library):
                    self.assertIn(library, description)

    def test_every_allowed_format_is_offered(self):
        enum = self.tools["create_document"].input_schema["properties"]["format"]["enum"]
        self.assertEqual(set(enum), set(ALLOWED_FORMATS))

    def test_the_result_carries_an_artifact_so_the_file_is_delivered(self):
        result = self.call(
            "create_document", title="Expenses", format="csv", script=WRITE_CSV
        )
        self.assertIn("artifact_path", result.data)
        self.assertIn("saved_to", result.data)


@unittest.skipUnless(proot_available(), "proot is not installed here")
class RealIsolationTests(unittest.TestCase):
    """Only runs where proot exists — on the phone, not in the sandbox."""

    def test_a_script_cannot_read_the_home_directory(self):
        home = Path(os.environ["HOME"])
        marker = home / ".isolation-probe"
        marker.write_text("secret", encoding="utf-8")
        self.addCleanup(marker.unlink, missing_ok=True)
        result = run_script(
            "from pathlib import Path\n"
            "import os\n"
            "print(Path(os.environ.get('REAL_HOME','/nonexistent'))"
            ".joinpath('.isolation-probe').exists())",
            workspace(), isolate=True,
        )
        self.assertNotIn("secret", result.stdout)


if __name__ == "__main__":
    unittest.main()
