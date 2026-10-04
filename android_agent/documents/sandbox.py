"""Run a model-written Python script without letting it reach anything.

This is the one place in the project where code the model wrote is executed,
so the containment is the feature. Four layers, in order of how much they
actually buy:

1. **proot hides the home directory.** `~/.env` holds the Telegram token, the
   LLM key and the Gmail app password; shared storage holds every photo the
   agent has taken. Binding an empty directory over `$HOME`, `/sdcard` and
   `/storage` removes all of it from the script's view while leaving
   `$PREFIX` — where Python and its libraries live — intact.
2. **Resource limits.** CPU seconds, address space, output file size and
   process count, so a runaway loop cannot fill the phone or flatten the
   battery.
3. **A scrubbed environment.** No `ANDROID_AGENT_*` variable is passed, so
   even without proot the script is not handed secrets directly.
4. **Output by copy.** The script writes inside its own workspace; the agent
   copies the result out. The script never holds a path into shared storage.

Workspaces live under `$PREFIX/tmp`, deliberately outside `$HOME`, so that
hiding the home directory does not also hide the script's own working
directory.
"""

from __future__ import annotations

import logging
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPT_NAME = "build.py"
DEFAULT_TIMEOUT = 60.0
#: A document script is not a long computation. These bound the damage a
#: mistake can do without getting in the way of a real report with a chart.
CPU_SECONDS = 50
ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_PROCESSES = 64
MAX_CAPTURED_OUTPUT = 8000

#: Paths hidden from the script. Everything the owner would mind losing.
HIDDEN_PATHS = ("/sdcard", "/storage")


@dataclass
class ScriptResult:
    ok: bool
    stdout: str
    stderr: str
    produced: list[Path] = field(default_factory=list)
    isolated: bool = False
    timed_out: bool = False

    @property
    def diagnostic(self) -> str:
        """What to tell the owner when a script fails.

        The traceback is the useful part, and it is the model's own output
        rather than device content, so showing it is safe and saves a round
        trip of guessing.
        """
        if self.timed_out:
            return f"The script did not finish within {DEFAULT_TIMEOUT:.0f} seconds."
        tail = (self.stderr or self.stdout or "").strip()
        return tail[-1500:] if tail else "The script produced no output and no error."


def proot_available() -> bool:
    return shutil.which("proot") is not None


def workspaces_root() -> Path:
    """Under $PREFIX/tmp, outside $HOME, so hiding home keeps this reachable."""
    prefix = os.environ.get("PREFIX")
    base = Path(prefix) / "tmp" if prefix else Path(tempfile.gettempdir())
    root = base / "android-agent-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_for(document_id: str) -> Path:
    """One directory per document: scripts cannot read each other's files."""
    workspace = workspaces_root() / document_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _clean_environment(workspace: Path) -> dict[str, str]:
    """Keep what Python needs to start; drop everything about the agent."""
    keep = ("PATH", "PREFIX", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "TMPDIR", "TZ")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    # HOME points at the workspace: scripts and libraries that write caches
    # should do it here, not in the real home directory.
    env["HOME"] = str(workspace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLBACKEND"] = "Agg"  # matplotlib must never try to open a display
    return env


def _apply_limits() -> None:
    """Run in the child between fork and exec."""
    for limit, value in (
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_AS, ADDRESS_SPACE_BYTES),
        (resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES),
        (resource.RLIMIT_NPROC, MAX_PROCESSES),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            # A limit the platform will not accept is not worth aborting for;
            # the remaining layers still apply.
            pass


def build_command(workspace: Path, *, isolate: bool) -> list[str]:
    """The argv used to run the script, with or without proot."""
    python = sys.executable or "python"
    inner = [python, SCRIPT_NAME]
    if not isolate:
        return inner

    blind = workspace / ".blind"
    blind.mkdir(exist_ok=True)
    command = ["proot"]
    home = os.environ.get("HOME")
    if home:
        # The whole point: ~/.env, the state directory and the repo all
        # disappear from the script's view.
        command += ["-b", f"{blind}:{home}"]
    for path in HIDDEN_PATHS:
        if Path(path).exists():
            command += ["-b", f"{blind}:{path}"]
    command += ["-w", str(workspace)]
    return command + inner


def clear_workspace(workspace: Path) -> None:
    """Empty the workspace without removing it.

    Each run starts clean. A reused workspace would still hold the previous
    version's output, which is then indistinguishable from a file this run
    produced - so revision 2 looked like a script that wrote nothing.
    """
    if not workspace.is_dir():
        return
    for entry in workspace.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            logger.debug("Could not clear %s from the workspace", entry.name)


def run_script(
    script: str,
    workspace: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    isolate: bool | None = None,
) -> ScriptResult:
    """Write the script into the workspace and run it there."""
    workspace.mkdir(parents=True, exist_ok=True)
    clear_workspace(workspace)
    script_path = workspace / SCRIPT_NAME
    script_path.write_text(script, encoding="utf-8")

    isolated = proot_available() if isolate is None else isolate
    command = build_command(workspace, isolate=isolated)
    before = {path.name for path in workspace.iterdir() if path.is_file()}

    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            env=_clean_environment(workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            preexec_fn=_apply_limits,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Document script timed out after %.0fs", timeout)
        return ScriptResult(False, "", "", isolated=isolated, timed_out=True)
    except FileNotFoundError:
        return ScriptResult(
            False, "", f"{command[0]} is not installed.", isolated=isolated
        )
    except OSError as exc:
        return ScriptResult(False, "", f"{type(exc).__name__}", isolated=isolated)

    stdout = completed.stdout.decode("utf-8", "replace")[:MAX_CAPTURED_OUTPUT]
    stderr = completed.stderr.decode("utf-8", "replace")[:MAX_CAPTURED_OUTPUT]
    produced = sorted(
        path
        for path in workspace.iterdir()
        if path.is_file() and path.name not in before and path.name != SCRIPT_NAME
    )
    return ScriptResult(
        ok=completed.returncode == 0,
        stdout=stdout,
        stderr=stderr,
        produced=produced,
        isolated=isolated,
    )


def pick_output(produced: list[Path], fmt: str) -> Path | None:
    """Choose the file the script meant as its result.

    Prefers the documented name, then any file with the right extension, so a
    script that writes `report.pdf` instead of `output.pdf` still works rather
    than failing on a naming technicality.
    """
    wanted = f".{fmt.lower()}"
    exact = [path for path in produced if path.name.lower() == f"output{wanted}"]
    if exact:
        return exact[0]
    matching = [path for path in produced if path.suffix.lower() == wanted]
    if matching:
        return max(matching, key=lambda path: path.stat().st_size)
    return None
