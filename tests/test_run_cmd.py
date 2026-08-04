# SPDX-License-Identifier: MPL-2.0
"""Execution tests for the Windows launcher, `scripts/run.cmd`.

The wrapper in `tests/contract/run.sh` is tested by *running* it
(`tests/contract/test_contract_meta.py`), added for #273 after tests that
spawned `sys.executable` directly let a broken wrapper stay green. The same
reasoning applies here, and more sharply: CI is ubuntu-only, so nothing else in
this repository ever executes `run.cmd`. These tests skip on non-Windows exactly
as `_shim_dir` does — the platform gate is the established pattern, not an
excuse.

Every case here is confined to two harmless branches: the ones that refuse
before provisioning, and `VERINOTE_DRY_RUN=1`, which reports the resolution and
exits. That confinement is deliberate. `run.cmd` derives the repository from its
own location (`%~dp0..`), which no fixture can redirect, so a test that reached
the install branch would run a real `pip install -e` into the developer's own
`.venv` — minutes of network I/O and a mutation of a known-good environment, out
of a unit test. `VERINOTE_VENV` is pointed at `tmp_path` besides, so even a
regression that slipped past the dry-run gate could not touch it.

What is deliberately *not* tested here: the venv-creation and install path
itself. It cannot be exercised without the side effects above, so it is verified
by hand on a Windows machine when the script changes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CMD = REPO_ROOT / "scripts" / "run.cmd"

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="run.cmd is a batch file; cmd.exe is not available on this platform"
)


def _run(tmp_path: Path, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Invoke run.cmd with a sandboxed venv location and no interactive pause."""
    env = os.environ | {
        "VERINOTE_VENV": str(tmp_path / "venv"),
        "VERINOTE_NO_PAUSE": "1",
    }
    # A stale VERINOTE_ROOT from the ambient shell would not change the script's
    # behaviour, but drop it so a failure here can never be blamed on one.
    env.pop("VERINOTE_ROOT", None)
    env.pop("PYTHON", None)
    env.pop("VERINOTE_DRY_RUN", None)
    env.pop("VERINOTE_EXTRAS", None)
    env.update(overrides)
    return subprocess.run(
        [str(RUN_CMD), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _shim(tmp_path: Path, name: str, body: str) -> Path:
    """A fake interpreter that answers the script's version query and nothing else.

    `run.cmd` asks for the version with `-c "import sys;print(...)"` and reads
    two whitespace-separated tokens, so a batch file that echoes them is a
    complete stand-in — and cannot accidentally build a virtualenv.
    """
    path = tmp_path / name
    # CRLF: cmd.exe reads a batch file by byte offset as it executes it.
    path.write_bytes(f"@echo off\r\n{body}\r\n".encode())
    return path


def test_script_never_assigns_verinote_root() -> None:
    """The launcher must not default VERINOTE_ROOT.

    That variable takes precedence over the active KB saved by the browser
    picker (`verinote/cli.py`, `_config_for`), and `verinote/web/app.py` refuses
    to save an active KB while it is set — so a root defaulted by a launcher
    would shadow the user's KB *and* remove the UI's way of correcting it. The
    failure is silent, which is why it is pinned here rather than left to review.

    Matched anywhere in the line, not just at its start: the spelling a future
    edit would most naturally reach for is the guarded one this script already
    uses twice for its own variables, `if not defined X set "X=..."`. An
    assertion anchored to the start of the line would miss exactly that.
    """
    assign = re.compile(r'\bsetx?\s+"?verinote_root\b', re.IGNORECASE)
    text = RUN_CMD.read_text(encoding="utf-8")
    assignments = [line.strip() for line in text.splitlines() if assign.search(line)]
    assert assignments == []


@windows_only
def test_dry_run_resolves_an_interpreter(tmp_path: Path) -> None:
    result = _run(tmp_path, VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run" in result.stdout
    # The resolution must name a concrete interpreter, not an empty variable.
    interpreter = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("interpreter")]
    assert len(interpreter) == 1, result.stdout
    assert interpreter[0].split(None, 1)[1].strip()


@windows_only
def test_refuses_python_below_the_floor(tmp_path: Path) -> None:
    """3.10 is refused outright: pyproject requires >=3.11 and the install would fail anyway."""
    shim = _shim(tmp_path, "py310.cmd", "echo 3 10")
    result = _run(tmp_path, PYTHON=str(shim))
    assert result.returncode != 0
    assert "3.10" in result.stdout
    assert "3.11" in result.stdout


@windows_only
def test_warns_but_proceeds_above_the_tested_ceiling(tmp_path: Path) -> None:
    """Newer than the CI matrix warns; it must not refuse.

    `requires-python = ">=3.11"` has no upper bound, so a launcher that refused
    here would enforce a stricter contract than the package it installs.
    """
    shim = _shim(tmp_path, "py399.cmd", "echo 3 99")
    result = _run(tmp_path, PYTHON=str(shim), VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Warning" in result.stdout
    assert "3.99" in result.stdout


@windows_only
def test_rejects_an_interpreter_that_cannot_report_a_version(tmp_path: Path) -> None:
    """An explicit PYTHON that answers nothing is an error, never a fall-through.

    Silently discovering a different interpreter would run something other than
    the one the caller named.
    """
    shim = _shim(tmp_path, "pymute.cmd", "rem answers nothing")
    result = _run(tmp_path, PYTHON=str(shim), VERINOTE_DRY_RUN="1")
    assert result.returncode != 0
    assert "version" in result.stdout.lower()


@windows_only
def test_reports_when_no_python_is_available(tmp_path: Path) -> None:
    """With no interpreter on PATH the script says so and names PYTHON as the way out.

    The message itself is asserted, not just the word `PYTHON`: that substring
    also appears in the "could not report its version" refusal, so matching it
    alone could not tell the two failures apart.
    """
    result = _run(tmp_path, PATH=str(tmp_path / "empty"))
    assert result.returncode != 0
    assert "no usable Python found" in result.stdout
    assert "PYTHON" in result.stdout


@windows_only
def test_existing_venv_short_circuits_discovery(tmp_path: Path) -> None:
    """An existing venv is used as-is, without consulting the `py` launcher.

    This is what stops a venv built with 3.12 from silently rebasing onto
    whatever `py -3` resolves to later — on this project's own dev machine those
    are different interpreters.
    """
    venv_python = tmp_path / "venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")  # never executed: the dry run stops before use
    result = _run(tmp_path, VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(venv_python) in result.stdout


@windows_only
def test_runs_from_a_path_containing_shell_metacharacters(tmp_path: Path) -> None:
    """A parenthesised install path must not abort the script.

    `C:\\Program Files (x86)\\...` and `...\\Repo (1)\\...` are ordinary paths.
    The double-click probe expands `%cmdcmdline%`, so unstripped quotes let the
    path's own `(`/`)` reach the parser, which aborted the launcher before
    anything ran — in precisely the double-click case the probe exists for.
    """
    nasty = tmp_path / "Repo (1)" / "scripts"
    nasty.mkdir(parents=True)
    shutil.copy2(RUN_CMD, nasty / "run.cmd")
    env = os.environ | {
        "VERINOTE_VENV": str(tmp_path / "venv"),
        "VERINOTE_NO_PAUSE": "1",
        "VERINOTE_DRY_RUN": "1",
    }
    # Same ambient-variable pops as `_run`: an inherited VERINOTE_EXTRAS holding
    # a metacharacter would fail this test for an entirely unrelated reason.
    for name in ("VERINOTE_ROOT", "PYTHON", "VERINOTE_EXTRAS"):
        env.pop(name, None)
    # The double-click spelling specifically: `cmd /c ""<path>" "`.
    result = subprocess.run(
        f'cmd /c ""{nasty / "run.cmd"}" "',
        capture_output=True,
        text=True,
        env=env,
        shell=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run" in result.stdout


@windows_only
def test_rejects_extras_containing_metacharacters(tmp_path: Path) -> None:
    """A metacharacter in VERINOTE_EXTRAS is refused rather than executed.

    It reaches an `echo` line and the stamp file, where `&` would truncate the
    stamp so that no later launch could ever match it — silently reinstalling
    on every single run.
    """
    result = _run(tmp_path, VERINOTE_EXTRAS="ingest&echo INJECTED", VERINOTE_DRY_RUN="1")
    assert result.returncode != 0
    assert "INJECTED" not in result.stdout
    assert "VERINOTE_EXTRAS" in result.stdout
