# SPDX-License-Identifier: MPL-2.0
"""State for the test-environment sandbox, shared by `conftest.py` and its tests.

This lives in its own module rather than in `conftest.py` on purpose: pytest
imports a conftest as the top-level module `conftest`, so a test doing
`from tests.conftest import ...` would create a *second* module object with its
own copy of the state. Both the conftest and the tests import this module under
the same name, so there is exactly one.
"""

from __future__ import annotations

import errno
import hashlib
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

    Every kind except `missing` carries a `fingerprint` of its `lstat`, so a
    change that keeps the kind — a rewrite *inside* a pre-run directory, a chmod
    of the real `app.json` — is still visible. Without it those compared equal to
    no change at all and the canary stayed silent. The kinds we cannot read back
    (`directory`, `unreadable_file`, `unknown`) carry *only* a fingerprint:
    detection, never reconstruction. Restoring them is refused (see `restore`).

    `missing` means one thing only: `lstat` said ENOENT/ENOTDIR — there is
    provably nothing there. A path we merely *failed to read* (EACCES on the
    parent, EIO, ELOOP) is `unknown`, never `missing`, because `missing` is
    restorable and restoring it means `_remove`. Collapsing "I could not look"
    into "there is nothing there" is how a canary deletes the file it exists to
    protect.
    """

    kind: str  # "missing" | "file" | "symlink" | "directory" | "unreadable_file" | "unknown"
    data: bytes | None = None  # kind == "file"
    target: str | None = None  # kind == "symlink"
    fingerprint: str | None = None  # every kind but "missing"


MISSING = Entry("missing")

# Only these can be put back. A whitelist, not a blacklist: a kind added later
# is refused by default rather than silently handed to `_remove`. The invariant
# behind the whitelist is that every kind in it was *read*, byte for byte
# (`file`), link for link (`symlink`), or proven absent by errno (`missing`).
RESTORABLE_KINDS = frozenset({"missing", "file", "symlink"})

# `lstat` failing with one of these — and only these — proves nothing is there.
# ENOENT: no such entry. ENOTDIR: a parent component is not a directory, so the
# path cannot exist. Every other errno means the lookup failed, not that the
# entry is absent.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})

# Bounds on the directory fingerprint. It runs against whatever unknown thing
# sits at the user's real config path, so it must stay cheap: two levels deep and
# a capped fan-out, rather than an unbounded walk of a user directory.
_DIR_MAX_DEPTH = 2
_DIR_MAX_ENTRIES = 256


def _stat_fingerprint(st: os.stat_result) -> str:
    """The portable, quiet-by-construction part of `lstat`.

    `st_mode` (type *and* permission bits), `st_size` and `st_mtime_ns` only.
    None of them moves unless something actually changed the entry, so they cannot
    make the canary cry wolf: reading a file does not touch mtime (atime does move
    on a read, which is exactly why it is excluded), and low-resolution
    filesystems can only make mtime miss a change, never invent one. `st_ino` /
    `st_dev` are left out for the opposite reason — on some network filesystems
    they are not stable across `stat` calls, which would turn every session into a
    phantom leak.
    """
    return f"mode=0o{st.st_mode:o} size={st.st_size} mtime_ns={st.st_mtime_ns}"


def _listing(path: Path, depth: int) -> tuple:
    """The `lstat` of every child of `path`, recursing `_DIR_MAX_DEPTH` levels down.

    A structured tuple, not a joined string: a child named `a,b:0o0:0:0` must not
    be able to forge the digest of a directory that holds different children (the
    same separator-forgery bug the repo fixed in #174).
    """
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return (("<listing-failed>", exc.errno),)
    items: list[tuple] = []
    for name in names[:_DIR_MAX_ENTRIES]:
        child = path / name
        try:
            cst = child.lstat()
        except OSError as exc:
            items.append((name, "lstat-failed", exc.errno))
            continue
        item = (name, cst.st_mode, cst.st_size, cst.st_mtime_ns)
        if depth < _DIR_MAX_DEPTH and stat.S_ISDIR(cst.st_mode):
            item = (*item, _listing(child, depth + 1))
        items.append(item)
    if len(names) > _DIR_MAX_ENTRIES:
        items.append(("<truncated>", len(names)))
    return tuple(items)


def _dir_fingerprint(path: Path, st: os.stat_result) -> str:
    """A *bounded-depth* listing of a directory: names plus each entry's `lstat` digest.

    No file contents are read and no symlink is followed — this runs against
    whatever unknown thing sits at the user's real config path, so it must stay
    cheap and incurious.

    What it catches, exactly:

    * anything created or deleted at depth 1 or 2 (`app.json/leaked.json`,
      `app.json/sub/leaked.json`);
    * anything *modified* at depth 1 or 2 — a rewrite, a resize, a chmod, a
      re-stamp — because each entry's own `lstat` is in the digest
      (`app.json/sub/app.json` rewritten in place).

    What it does not catch, honestly: a modification at depth 3 or deeper
    (`app.json/a/b/c.json` rewritten in place) — the depth-3 entry's `lstat` is
    not in the digest and rewriting a file moves no ancestor's mtime. Creations
    and deletions *do* still show at any depth, because they move the mtime of
    their parent directory, which is itself an entry at depth ≤ 2... up to depth 3
    only. Beyond that the canary is blind, by choice: `_DIR_MAX_DEPTH` buys the
    bound. A directory wider than `_DIR_MAX_ENTRIES` is digested by its first
    `_DIR_MAX_ENTRIES` names plus its total count.
    """
    payload = repr((_stat_fingerprint(st), _listing(path, 1)))
    return "dir:" + hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def snapshot(path: Path) -> Entry:
    """Describe what is at `path` without following symlinks, and without raising.

    The canary must never blow up on the way to its own assertion: an unreadable
    path or a directory where the file should be means "not something we can put
    back", not "crash the session".

    Note the asymmetry that matters: a *failed* lookup becomes `unknown`
    (unrestorable), not `missing` (restorable, and restored by deleting). Only an
    errno that proves absence yields `missing`.
    """
    try:
        st = path.lstat()
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return MISSING
        # We did not learn what is here — only that we could not look. Anything we
        # cannot read, we must not delete.
        return Entry("unknown", fingerprint=f"lstat_failed:{exc.errno}")
    if stat.S_ISLNK(st.st_mode):
        try:
            return Entry("symlink", target=os.readlink(path), fingerprint=_stat_fingerprint(st))
        except OSError:
            return Entry("unknown", fingerprint=_stat_fingerprint(st))
    if stat.S_ISREG(st.st_mode):
        try:
            return Entry("file", data=path.read_bytes(), fingerprint=_stat_fingerprint(st))
        except OSError:
            return Entry("unreadable_file", fingerprint=_stat_fingerprint(st))
    if stat.S_ISDIR(st.st_mode):
        return Entry("directory", fingerprint=_dir_fingerprint(path, st))
    return Entry("unknown", fingerprint=_stat_fingerprint(st))


def _fingerprint_mode(fingerprint: str | None) -> int | None:
    """The permission bits recorded in a `_stat_fingerprint`, or None if unreadable."""
    if fingerprint is None:
        return None
    for field in fingerprint.split():
        if field.startswith("mode=0o"):
            try:
                return stat.S_IMODE(int(field[len("mode=0o") :], 8))
            except ValueError:
                return None
    return None


def _remove(path: Path) -> None:
    """Delete whatever is at `path` — file, symlink, or directory — following nothing.

    Only ever reached for a kind in `RESTORABLE_KINDS`, i.e. for a path whose
    pre-run state we actually read.
    """
    try:
        st = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        # `unlink` removes the symlink itself, never its target.
        path.unlink(missing_ok=True)


def _write_atomically(path: Path, data: bytes, mode: int | None) -> None:
    """Replace `path` with `data` in one step, so a crash cannot leave it half-written.

    `mkstemp` creates its file 0o600, so restoring without `chmod` would silently
    re-permission the user's config. Put the recorded mode back.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-sandbox-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            if mode is not None:
                # Through the fd, so the mode lands on *this* file and not on whatever
                # a racing leak might swap into `tmp`'s name. `os.replace` keeps it.
                os.fchmod(handle.fileno(), mode)
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

    A `directory`, an `unreadable_file` or an `unknown` entry is the one thing we
    cannot reconstruct: we only ever learned a fingerprint of it (or, for a failed
    `lstat`, not even that), never its contents, so removing it would destroy what
    we came to protect. Refuse instead, and let the caller report it. Detecting
    such a change (which the fingerprint now does) and *repairing* it are separate
    powers — the canary gained the first without taking the second.
    """
    if before.kind not in RESTORABLE_KINDS:
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
    _write_atomically(path, before.data, _fingerprint_mode(before.fingerprint))
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
