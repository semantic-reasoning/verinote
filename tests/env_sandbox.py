# SPDX-License-Identifier: MPL-2.0
"""State for the test-environment sandbox, shared by `conftest.py` and its tests.

This lives in its own module rather than in `conftest.py` on purpose: pytest
imports a conftest as the top-level module `conftest`, so a test doing
`from tests.conftest import ...` would create a *second* module object with its
own copy of the state. Both the conftest and the tests import this module under
the same name, so there is exactly one.
"""

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


def snapshot(path: Path) -> bytes | None:
    """Return the file's bytes, or None when it is not a readable file.

    The canary must never blow up on the way to its own assertion: an
    unreadable path (`PermissionError`), a directory where the file should be
    (`IsADirectoryError`), or a missing parent (`NotADirectoryError`) all mean
    "no config bytes to compare", not "crash the session".
    """
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None


def restore(path: Path, before: bytes | None) -> None:
    """Put a leaked-into file back the way it was: rewrite the bytes, or remove it."""
    if before is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(before)


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
