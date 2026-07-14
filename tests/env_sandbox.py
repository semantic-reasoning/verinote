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
from collections.abc import Callable
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
# sits at the user's real config path, so it must stay cheap: two levels deep,
# rather than an unbounded walk of a user directory.
_DIR_MAX_DEPTH = 2

# How many children the listing keeps *as a tuple*. Everything past it is folded
# into a rolling digest instead (see `_listing`), so this bounds the memory the
# fingerprint holds, not what it can see. It used to bound both — the listing was
# truncated to the first `_DIR_MAX_ENTRIES` names plus a count — which made an
# in-place edit of the 300th child of a 300-child directory invisible: the count
# does not move, the entry is not in the tuple, and editing a file moves no
# parent's mtime. A detection bound that nobody can see is how a canary lies.
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

    *Every* child is digested. The children past `_DIR_MAX_ENTRIES` are folded into
    one rolling sha256 rather than kept as tuples, which caps the memory a very wide
    directory can cost without capping what the canary can notice in it. `sorted`
    makes that split deterministic.
    """
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return (("<listing-failed>", exc.errno),)
    items: list[tuple] = []
    overflow = hashlib.sha256()
    overflow_count = 0
    for index, name in enumerate(names):
        child = path / name
        try:
            cst = child.lstat()
        except OSError as exc:
            item: tuple = (name, "lstat-failed", exc.errno)
        else:
            item = (name, cst.st_mode, cst.st_size, cst.st_mtime_ns)
            if depth < _DIR_MAX_DEPTH and stat.S_ISDIR(cst.st_mode):
                item = (*item, _listing(child, depth + 1))
        if index < _DIR_MAX_ENTRIES:
            items.append(item)
            continue
        overflow_count += 1
        overflow.update(repr(item).encode("utf-8", "surrogatepass"))
    if overflow_count:
        items.append(("<overflow>", overflow_count, overflow.hexdigest()))
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
    and deletions *do* still show down to depth 3, because they move the mtime of
    their parent directory, which is itself an entry at depth ≤ 2. Deeper than
    that the canary is blind, by choice: `_DIR_MAX_DEPTH` buys the bound, and
    `test_the_directory_fingerprint_stops_at_a_documented_depth` pins it.

    Width, unlike depth, is *not* a bound on detection: every child is digested at
    every level the depth bound allows, however many there are (`_DIR_MAX_ENTRIES`
    only decides which ones are kept as tuples and which are folded into a rolling
    hash).
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

    Only ever reached to restore `missing`, i.e. for a path that provably held
    nothing before the run (see `_ABSENT_ERRNOS`), and to clear a directory the run
    dropped where a file belongs (see `_clear_the_way`). Never for anything whose
    pre-run contents we did not read.
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


def _apply_mode(fd: int, tmp: str, mode: int | None) -> None:
    """Best-effort chmod of the staged replacement. Never fatal.

    `mkstemp` creates its file 0o600, so restoring without a chmod would silently
    re-permission the user's config — but the mode is the *incidental* half of a
    restore and the bytes are the point. Two ways this used to be able to eat the
    file it was protecting, back when `restore` deleted before it wrote:

    * `os.fchmod` does not exist on Windows, a platform `verinote.config` explicitly
      supports. Calling it unguarded raised `AttributeError` — after the delete.
    * a chmod can fail for its own reasons (EPERM on a file the run chowned away).

    So: prefer the fd (the mode then lands on *this* file, not on whatever a racing
    leak might swap into `tmp`'s name, and `os.replace` carries it over), fall back
    to the path where there is no `fchmod`, and swallow the failure either way. A
    restore that put every byte back but not the mode beats one that raised.
    """
    if mode is None:
        return
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(tmp, mode)
    except OSError:
        pass


def _stage(path: Path, build: Callable[[int, str], None]) -> None:
    """Build the replacement under a private name, then `os.replace` it onto `path`.

    The whole point of the ordering. `os.replace` overwrites a file or a symlink
    atomically and in place, so `path` holds the old entry right up until it holds
    the new one — there is no instant at which it holds neither. The previous
    version removed `path` first and wrote second, which turned *any* failure in
    between — `mkstemp` hitting EACCES on a read-only parent, ENOSPC, a chmod
    raising, `os.replace` itself failing — into the permanent loss of the user's
    real `app.json`. Nothing is deleted here on the way to a write.

    A raise still means the restore failed, but it now means it failed with the
    entry it was restoring still on disk. `build` is handed the `mkstemp` fd and
    owns it: it closes it, on every path out.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-sandbox-")
    try:
        build(fd, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_file(fd: int, tmp: str, data: bytes, mode: int | None) -> None:
    """Fill the staged file. `fdopen` owns the fd from here and closes it on any exit."""
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        _apply_mode(handle.fileno(), tmp, mode)


def _write_symlink(fd: int, tmp: str, target: str) -> None:
    """Turn the staged (regular, empty) file into the symlink we mean to put back."""
    os.close(fd)
    os.unlink(tmp)
    os.symlink(target, tmp)


def _clear_the_way(path: Path) -> None:
    """Remove a *directory* squatting where a file or a symlink has to go. Nothing else.

    The one delete `os.replace` cannot spare us: it refuses to overwrite a
    directory. And it is never the user's own entry — this only runs when the
    pre-run kind was `file` or `symlink`, i.e. when we hold the bytes to put back,
    so a directory here now is something the run itself created.
    """
    try:
        st = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode):  # `lstat`, so a symlink *to* a directory is not one
        shutil.rmtree(path, ignore_errors=True)


def restore(path: Path, before: Entry) -> bool:
    """Put `path` back the way `before` describes it. False when that is impossible.

    A leak can leave anything at the path — a file, a symlink pointing at the
    user's real KB config, or a directory. The replacement is therefore staged
    beside the path and moved *onto* it (`_stage`), never written *through* it:
    writing through the path would follow a planted symlink, corrupt whatever it
    aims at, and leave the leak itself in place.

    Restoring a `file` or a `symlink` deletes nothing. `os.replace` overwrites the
    leak — file or symlink alike — in one atomic step, so a failure anywhere in
    here leaves the path holding *something*, never nothing. The single exception
    is a directory sitting where the file belongs, which `os.replace` cannot
    overwrite (`_clear_the_way`), and which by construction is the run's own litter
    and not the user's entry. Restoring `missing` is the one kind whose restoration
    *is* a delete — and `missing` means an errno proved there was nothing here.

    A `directory`, an `unreadable_file` or an `unknown` entry is the one thing we
    cannot reconstruct: we only ever learned a fingerprint of it (or, for a failed
    `lstat`, not even that), never its contents, so removing it would destroy what
    we came to protect. Refuse instead, and let the caller report it. Detecting
    such a change (which the fingerprint now does) and *repairing* it are separate
    powers — the canary gained the first without taking the second.
    """
    if before.kind not in RESTORABLE_KINDS:
        return False
    if before.kind == "missing":
        _remove(path)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear_the_way(path)
    if before.kind == "symlink":
        assert before.target is not None
        target = before.target
        _stage(path, lambda fd, tmp: _write_symlink(fd, tmp, target))
        return True
    assert before.data is not None
    data, mode = before.data, _fingerprint_mode(before.fingerprint)
    _stage(path, lambda fd, tmp: _write_file(fd, tmp, data, mode))
    return True


# The baseline for the real app config, taken at *import* time. It has to be this
# early: `conftest` imports this module before `pytest_configure`, before
# collection, and before any test module's import-time code, so a leak from any
# of those is still measured against the user's true pre-run state. Capturing it
# in a session fixture instead would bake an import-time leak into the baseline —
# the run would then look clean and restore nothing.
REAL_APP_CONFIG_BEFORE = snapshot(REAL_APP_CONFIG_PATH)


def _park(path: Path, before: Entry) -> str:
    """Put the pre-run bytes somewhere the user can find them, for a restore that failed.

    The worst outcome is not a canary that cannot repair the file — it is a canary
    that fails holding the only copy of it and takes it to the grave. Whatever went
    wrong, the bytes we read at import time are still in memory here, so they get
    written somewhere outside the config directory (which may be exactly what is
    broken) before we say a word.
    """
    if before.data is None:
        return "its pre-run contents were never readable, so there is nothing to hand back"
    try:
        fd, name = tempfile.mkstemp(prefix="verinote-app-json-rescue-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(before.data)
    except OSError as exc:
        return f"its pre-run bytes could not be parked either ({exc}); they are: {before.data!r}"
    return f"its pre-run bytes are parked in {name} — copy that back over {path} by hand"


def leak_report(path: Path, before: Entry) -> str | None:
    """Restore a leaked-into path and return why it failed the run, or None if clean.

    Never raises. It is called from `pytest_sessionfinish`, where an exception is an
    INTERNALERROR that takes the whole session down — and it is called at the one
    moment the user's real config may be mid-repair, so "the canary died" and "the
    canary died holding your file" must not be the same event.
    """
    after = snapshot(path)
    if after == before:
        return None
    verb = "created" if before.kind == "missing" else "modified"
    try:
        restored = restore(path, before)
        why = f"it was a {before.kind} before the run"
    except Exception as exc:  # noqa: BLE001 - a failed restore must still be *reported*
        restored = False
        why = f"restoring it raised {exc!r}"
    tail = (
        "it has been restored to its pre-run state"
        if restored
        else f"IT COULD NOT BE RESTORED ({why}) — {_park(path, before)}"
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
