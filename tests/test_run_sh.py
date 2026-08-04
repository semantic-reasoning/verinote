# SPDX-License-Identifier: MPL-2.0
"""Execution tests for the macOS/Linux launcher, `scripts/run.sh`.

The mirror of `tests/test_run_cmd.py`, with one difference that changes what
these tests are worth: CI is ubuntu-only (`.github/workflows/ci.yml`), so
`run.cmd`'s suite skips wholesale on every machine that runs it, while this one
actually executes. The precedent for running a shell launcher for real rather
than re-implementing its logic in Python is
`tests/contract/test_contract_meta.py` (issue #273), which caught a broken
wrapper that every test spawning `sys.executable` directly had let stay green.

Every case here is confined to the same two harmless branches as the Windows
suite: the ones that refuse before provisioning, and `VERINOTE_DRY_RUN=1`, which
reports the resolution and exits. That confinement is deliberate. `run.sh`
derives the repository from its own location, which no fixture can redirect, so
a test that reached the install branch would run a real `pip install -e` into
the developer's own `.venv` -- minutes of network I/O and a mutation of a
known-good environment, out of a unit test. `VERINOTE_VENV` is pointed at
`tmp_path` besides, so even a regression that slipped past the dry-run gate
could not touch it.

What is deliberately *not* tested here: the venv-creation and install path
itself, and the argument passthrough that follows it, since both are reachable
only through the side effects above.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "scripts" / "run.sh"

# The externals `run.sh` needs before it has found an interpreter: `bash` for
# the `/usr/bin/env bash` shebang, and `dirname` to locate the repository. Kept
# in step with `WRAPPER_TOOLS` in tests/contract/test_contract_meta.py, which
# pins PATH the same way for the same reason.
LAUNCHER_TOOLS = ("bash", "dirname")

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="run.sh is a POSIX shell script; bash is not assumed on Windows"
)


def _run(tmp_path: Path, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Invoke run.sh with a sandboxed venv location."""
    env = os.environ | {"VERINOTE_VENV": str(tmp_path / "venv")}
    # A stale VERINOTE_ROOT from the ambient shell would not change the script's
    # behaviour, but drop it so a failure here can never be blamed on one.
    for name in ("VERINOTE_ROOT", "PYTHON", "VERINOTE_DRY_RUN", "VERINOTE_EXTRAS",
                 "VERINOTE_REINSTALL"):
        env.pop(name, None)
    env.update(overrides)
    return subprocess.run(
        [str(RUN_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams. Diagnostics go to stderr, the dry-run report to stdout."""
    return result.stdout + result.stderr


def _shim(tmp_path: Path, name: str, body: str) -> Path:
    """A fake interpreter that answers the script's version query and nothing else.

    `run.sh` asks for the version with `-c "import sys; print(...)"` and reads
    two whitespace-separated tokens, so a shell script that echoes them is a
    complete stand-in -- and cannot accidentally build a virtualenv.
    """
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _toolsdir(tmp_path: Path) -> Path:
    """A PATH directory exposing the launcher's externals and *no* interpreter.

    Pinning PATH to this is what makes the discovery cases hold everywhere: the
    ambient PATH on any dev machine has a `python3`, so a test that relied on it
    could not distinguish "discovery worked" from "discovery was never needed".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in LAUNCHER_TOOLS:
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"{tool} is not available; run.sh cannot run on this platform")
        (bin_dir / tool).symlink_to(real)
    return bin_dir


def test_script_never_assigns_verinote_root() -> None:
    """The launcher must not default VERINOTE_ROOT.

    That variable takes precedence over the active KB saved by the browser
    picker (`verinote/cli.py`, `_config_for`), and `verinote/web/app.py` refuses
    to save an active KB while it is set -- so a root defaulted by a launcher
    would shadow the user's KB *and* remove the UI's way of correcting it. The
    failure is silent, which is why it is pinned here rather than left to
    review. The Windows launcher carries the identical guard.

    Both spellings are matched: a bare assignment and an `export`, since either
    would leak the variable into the app the script goes on to exec.
    """
    assign = re.compile(r"^\s*(export\s+)?VERINOTE_ROOT=", re.MULTILINE)
    text = RUN_SH.read_text(encoding="utf-8")
    assert assign.search(text) is None


def test_script_is_executable() -> None:
    """The mode bit is part of the deliverable.

    `scripts/run.sh` is documented as being run directly, so a checkout where
    git recorded 100644 turns the README's own command into "permission
    denied". Nothing else in the suite would notice: every case here invokes the
    script as itself, which is exactly the invocation that breaks.
    """
    assert os.access(RUN_SH, os.X_OK)


@posix_only
def test_dry_run_resolves_an_interpreter(tmp_path: Path) -> None:
    result = _run(tmp_path, VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, _output(result)
    assert "dry run" in result.stdout
    # The resolution must name a concrete interpreter, not an empty variable.
    interpreter = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("interpreter")]
    assert len(interpreter) == 1, result.stdout
    assert interpreter[0].split(None, 1)[1].strip(' "')


@posix_only
def test_refuses_python_below_the_floor(tmp_path: Path) -> None:
    """3.10 is refused outright: pyproject requires >=3.11 and the install would fail anyway."""
    shim = _shim(tmp_path, "py310", "echo 3 10")
    result = _run(tmp_path, PYTHON=str(shim))
    assert result.returncode != 0
    assert "3.10" in _output(result)
    assert "3.11" in _output(result)


@posix_only
def test_warns_but_proceeds_above_the_tested_ceiling(tmp_path: Path) -> None:
    """Newer than the CI matrix warns; it must not refuse.

    `requires-python = ">=3.11"` has no upper bound, so a launcher that refused
    here would enforce a stricter contract than the package it installs.
    """
    shim = _shim(tmp_path, "py399", "echo 3 99")
    result = _run(tmp_path, PYTHON=str(shim), VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, _output(result)
    assert "Warning" in _output(result)
    assert "3.99" in _output(result)


@posix_only
def test_rejects_an_interpreter_that_cannot_report_a_version(tmp_path: Path) -> None:
    """An explicit PYTHON that answers nothing is an error, never a fall-through.

    Silently discovering a different interpreter would run something other than
    the one the caller named.
    """
    shim = _shim(tmp_path, "pymute", ": answers nothing")
    result = _run(tmp_path, PYTHON=str(shim), VERINOTE_DRY_RUN="1")
    assert result.returncode != 0
    assert "version" in _output(result).lower()


@posix_only
def test_reports_when_no_python_is_available(tmp_path: Path) -> None:
    """With no interpreter on PATH the script says so and names PYTHON as the way out.

    The message itself is asserted, not just the word `PYTHON`: that substring
    also appears in the "could not report its version" refusal, so matching it
    alone could not tell the two failures apart.
    """
    result = _run(tmp_path, PATH=str(_toolsdir(tmp_path)))
    assert result.returncode != 0
    assert "no usable Python found" in _output(result)
    assert "PYTHON" in _output(result)


@posix_only
def test_honours_an_explicit_python_override(tmp_path: Path) -> None:
    """PYTHON must win over discovery.

    PATH here holds *no* interpreter at all, so the run can only succeed via the
    override -- if it were ignored, discovery would have nothing to fall back on
    and this would go red rather than pass by luck.
    """
    result = _run(
        tmp_path,
        PATH=str(_toolsdir(tmp_path)),
        PYTHON=sys.executable,
        VERINOTE_DRY_RUN="1",
    )
    assert result.returncode == 0, _output(result)
    assert sys.executable in result.stdout


@posix_only
def test_existing_venv_short_circuits_discovery(tmp_path: Path) -> None:
    """An existing venv is used as-is, without consulting PATH.

    This is what stops a venv built with 3.12 from silently rebasing onto
    whatever `python3` resolves to later -- on this project's own dev machine
    those are different interpreters. PATH is pinned bare to prove the point:
    discovery could not have supplied this answer.
    """
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")  # never executed: the dry run stops before use
    venv_python.chmod(venv_python.stat().st_mode | stat.S_IXUSR)
    result = _run(tmp_path, PATH=str(_toolsdir(tmp_path)), VERINOTE_DRY_RUN="1")
    assert result.returncode == 0, _output(result)
    assert str(venv_python) in result.stdout


@posix_only
def test_directory_without_an_interpreter_is_not_mistaken_for_a_venv(tmp_path: Path) -> None:
    """An interrupted `python -m venv` leaves the directory but no interpreter.

    The existence test is therefore the interpreter, never the directory. If it
    were the directory, this run would skip provisioning and go on to exec a
    `verinote` that was never installed.
    """
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    result = _run(tmp_path, PATH=str(_toolsdir(tmp_path)))
    assert result.returncode != 0
    assert "no usable Python found" in _output(result)


@posix_only
def test_runs_from_a_path_containing_shell_metacharacters(tmp_path: Path) -> None:
    """A parenthesised, spaced install path must not derail the script.

    `~/Downloads/verinote (1)/` is what an unzip of a second copy produces, and
    every path the script derives from its own location flows from there into a
    command line.
    """
    nasty = tmp_path / "Repo (1) & co" / "scripts"
    nasty.mkdir(parents=True)
    shutil.copy2(RUN_SH, nasty / "run.sh")
    # The script now refuses a location with no pyproject.toml beside it, which
    # is the point of the guard -- so the copy needs one to get as far as the
    # resolution this test is actually about.
    (nasty.parent / "pyproject.toml").write_text("", encoding="utf-8")
    env = os.environ | {"VERINOTE_VENV": str(tmp_path / "venv"), "VERINOTE_DRY_RUN": "1"}
    # Same ambient-variable pops as `_run`: an inherited VERINOTE_EXTRAS holding
    # a metacharacter would fail this test for an entirely unrelated reason.
    for name in ("VERINOTE_ROOT", "PYTHON", "VERINOTE_EXTRAS", "VERINOTE_REINSTALL"):
        env.pop(name, None)
    result = subprocess.run(
        [str(nasty / "run.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run" in result.stdout
    assert str(nasty.parent) in result.stdout


@posix_only
def test_refuses_when_the_repository_cannot_be_located(tmp_path: Path) -> None:
    """A mis-derived repository root is named, not acted on.

    Everything the launcher does is built from that root, and each way it can
    come out wrong is silent: with `dirname` absent from PATH the substitution
    is empty, so `cd /..` lands on `/` and succeeds. Without this guard the run
    reaches `pip install -e "/[anthropic,ingest]"` and fails talking about a
    requirement the user never typed.
    """
    stray = tmp_path / "not-a-checkout" / "scripts"
    stray.mkdir(parents=True)
    shutil.copy2(RUN_SH, stray / "run.sh")
    result = subprocess.run(
        [str(stray / "run.sh")],
        capture_output=True,
        text=True,
        env=os.environ | {"VERINOTE_VENV": str(tmp_path / "venv"), "VERINOTE_DRY_RUN": "1"},
        timeout=120,
    )
    assert result.returncode != 0
    assert "pyproject.toml" in _output(result)


@posix_only
def test_rejects_extras_that_are_not_extra_names(tmp_path: Path) -> None:
    """A metacharacter in VERINOTE_EXTRAS is refused rather than passed to pip.

    Unlike the Windows launcher, this is not an injection guard: `$extras` is
    only ever used inside a quoted expansion, and the shell does not re-parse
    the result of one. It is a validity check -- extras are PEP 508 identifiers,
    so this names the typo instead of letting pip fail with a message about a
    requirement the user never typed.
    """
    result = _run(tmp_path, VERINOTE_EXTRAS="ingest&echo INJECTED", VERINOTE_DRY_RUN="1")
    assert result.returncode != 0
    assert "INJECTED" not in _output(result)
    assert "VERINOTE_EXTRAS" in _output(result)
