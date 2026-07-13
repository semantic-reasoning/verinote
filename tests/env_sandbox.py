# SPDX-License-Identifier: MPL-2.0
"""State for the test-environment sandbox, shared by `conftest.py` and its tests.

This lives in its own module rather than in `conftest.py` on purpose: pytest
imports a conftest as the top-level module `conftest`, so a test doing
`from tests.conftest import ...` would create a *second* module object with its
own copy of the state. Both the conftest and the tests import this module under
the same name, so there is exactly one.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from verinote.config import app_config_dir

# Captured at import time — i.e. under the *ambient* environment, before the
# sandbox monkeypatches anything. These are the real paths the test run must
# never touch, and they are what makes the isolation regression tests
# non-vacuous.
REAL_APP_CONFIG_DIR = app_config_dir()
REAL_APP_CONFIG_PATH = REAL_APP_CONFIG_DIR / "app.json"

_sandbox_home: Path | None = None
_session_home: Path | None = None


@dataclass(frozen=True)
class Entry:
    """What sits at a path: nothing, a regular file, a symlink, or something else.

    The canary compares two of these, so the *kind* has to be part of the value.
    Reading through the path (`read_bytes`) would follow a symlink and report the
    target's bytes, which makes a leak that replaces `app.json` with a symlink
    look identical to no leak at all.
    """

    kind: str  # "missing" | "file" | "symlink" | "other"
    data: bytes | None = None  # kind == "file"
    target: str | None = None  # kind == "symlink"


MISSING = Entry("missing")


def snapshot(path: Path) -> Entry:
    """Describe what is at `path` without following symlinks, and without raising.

    The canary must never blow up on the way to its own assertion: an unreadable
    path, a directory where the file should be, or a missing parent all mean
    "nothing we can put back", not "crash the session".
    """
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return MISSING
    if stat.S_ISLNK(st.st_mode):
        try:
            return Entry("symlink", target=os.readlink(path))
        except OSError:
            return Entry("other")
    if stat.S_ISREG(st.st_mode):
        try:
            return Entry("file", data=path.read_bytes())
        except OSError:
            return Entry("other")
    return Entry("other")


def _remove(path: Path) -> None:
    """Delete whatever is at `path` — file, symlink, or directory — following nothing."""
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        # `unlink` removes the symlink itself, never its target.
        path.unlink(missing_ok=True)


def _write_atomically(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step, so a crash cannot leave it half-written."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-sandbox-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def restore(path: Path, before: Entry) -> bool:
    """Put `path` back the way `before` describes it. False when that is impossible.

    A leak can leave anything at the path — a file, a symlink pointing at the
    user's real KB config, or a directory — so the current entry is removed by
    kind before the original is recreated. Writing through the path instead would
    follow a planted symlink and corrupt whatever it aims at while leaving the
    leak itself in place.

    `other` (a directory, an unreadable file) is the one thing we cannot
    reconstruct: we never learned its contents, so removing it would destroy what
    we came to protect. Refuse instead, and let the caller report it.
    """
    if before.kind == "other":
        return False
    _remove(path)
    if before.kind == "missing":
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if before.kind == "symlink":
        assert before.target is not None
        path.symlink_to(before.target)
        return True
    assert before.data is not None
    _write_atomically(path, before.data)
    return True


# The baseline for the real app config, taken at *import* time. It has to be this
# early: `conftest` imports this module before `pytest_configure`, before
# collection, and before any test module's import-time code, so a leak from any
# of those is still measured against the user's true pre-run state. Capturing it
# in a session fixture instead would bake an import-time leak into the baseline —
# the run would then look clean and restore nothing.
REAL_APP_CONFIG_BEFORE = snapshot(REAL_APP_CONFIG_PATH)


def leak_report(path: Path, before: Entry) -> str | None:
    """Restore a leaked-into path and return why it failed the run, or None if clean."""
    after = snapshot(path)
    if after == before:
        return None
    restored = restore(path, before)
    verb = "created" if before.kind == "missing" else "modified"
    tail = (
        "it has been restored to its pre-run state"
        if restored
        else f"IT COULD NOT BE RESTORED (it was a {before.kind} before the run) — repair it by hand"
    )
    return (
        f"the test run {verb} the real app config at {path}; {tail}. "
        "The leak is a bug: a test escaped the environment sandbox."
    )


def session_home() -> Path | None:
    """The fake home sealed in at session start, or None if the seal is off.

    This is the tier that covers module/session-scoped fixtures and test-module
    import time, both of which run before any function-scoped `monkeypatch`.
    """
    return _session_home


def seal(home: Path) -> None:
    global _session_home
    _session_home = home


def unseal() -> None:
    global _session_home
    _session_home = None


def sandbox_home() -> Path | None:
    """The fake home installed for the running test, or None if the sandbox is off.

    Read through a function rather than a fixture argument so the isolation
    tests depend on the autouse fixture *implicitly*: requesting the fixture by
    name would re-enable it even with `autouse=True` deleted, and the regression
    tests would then prove nothing.
    """
    return _sandbox_home


def enter(home: Path) -> None:
    global _sandbox_home
    _sandbox_home = home


def exit() -> None:  # noqa: A001 - module-level verb, not the builtin
    global _sandbox_home
    _sandbox_home = None
