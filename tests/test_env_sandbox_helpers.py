# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the `env_sandbox` helpers, independent of FastAPI.

These exercise `snapshot`, `restore`, `leak_report`, `_listing`, `_park` and the
`Entry` value directly against `tmp_path` stand-ins — never the real config, and
never the web stack. They used to live in `test_env_isolation.py` behind that
module's `pytest.importorskip("fastapi")`, which meant a machine without FastAPI
skipped the harness's own self-tests wholesale. They need neither FastAPI nor the
autouse sandbox fixture, so they belong in a module that always runs. The
FastAPI- and fixture-dependent isolation tests stay in `test_env_isolation.py`.
"""

import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

import env_sandbox
from env_sandbox import (
    MISSING,
    Entry,
    leak_report,
    restore,
    snapshot,
)

# `os.geteuid` and `os.mkfifo` do not exist on Windows, and a marker's condition is
# evaluated at *import* time — calling them unguarded breaks collection on a
# platform `verinote.config` explicitly supports.
NEEDS_FIFOS = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="this platform has no FIFOs to snapshot"
)
NOT_ROOT = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root reads and traverses straight through mode 0o000",
)
NEEDS_LUTIMES = pytest.mark.skipif(
    os.utime not in os.supports_follow_symlinks,
    reason="this platform cannot stamp a symlink without following it",
)


def test_canary_restores_a_leaked_app_config_before_failing(tmp_path):
    # The canary does not just detect a leak; it undoes it. Exercised here
    # against a stand-in path — never the real one.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    path.write_bytes(b'{"active_root": "/tmp/pytest-kb"}\n')  # the leak
    message = leak_report(path, before)

    assert path.read_bytes() == original
    assert message is not None
    assert "modified" in message
    assert "COULD NOT BE RESTORED" not in message


def test_canary_restore_removes_a_config_the_run_created(tmp_path):
    path = tmp_path / "nested" / "app.json"
    before = snapshot(path)

    assert before == MISSING
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{}\n")  # the leak: a file that did not exist before
    message = leak_report(path, before)

    assert not path.exists()
    assert message is not None and "created" in message


def test_canary_replaces_a_symlink_without_writing_through_it(tmp_path):
    # The nastiest shape of the leak: the config is swapped for a symlink. Writing
    # the original bytes *through* the path would corrupt the symlink's target and
    # leave the symlink in place — so the real `app.json` would still be a link to
    # somebody else's file when the run ends.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"do not touch\n")

    path.unlink()
    path.symlink_to(victim)  # the leak
    message = leak_report(path, before)

    assert not path.is_symlink(), "the canary followed the symlink instead of replacing it"
    assert path.read_bytes() == original
    assert victim.read_bytes() == b"do not touch\n", "the canary wrote through the symlink"
    assert message is not None


def test_canary_removes_a_directory_left_where_the_config_was(tmp_path):
    path = tmp_path / "app.json"
    before = snapshot(path)

    assert before == MISSING
    (path / "nested").mkdir(parents=True)  # the leak: a directory at the config path
    message = leak_report(path, before)

    assert not path.exists()
    assert message is not None


@NOT_ROOT
def test_canary_reports_a_missing_restore_it_could_not_carry_out(tmp_path):
    # `missing` is the one kind whose restore *is* a delete, and `_remove` swallows
    # its own failures. If the delete is a no-op — the parent went unreadable
    # mid-run, so `_remove`'s `lstat` is blocked — the file the run leaked is still
    # there. The canary must re-check that the path is provably gone and report
    # "COULD NOT BE RESTORED", not claim a pre-run state it never reached.
    parent = tmp_path / "verinote"
    parent.mkdir()
    path = parent / "app.json"
    before = snapshot(path)

    assert before == MISSING

    path.write_bytes(b"leaked\n")  # the leak: a file where an errno proved nothing was
    parent.chmod(0o000)  # and now the restoring delete cannot even lstat it
    try:
        message = leak_report(path, before)
        parent.chmod(0o700)
        assert path.read_bytes() == b"leaked\n", "the leaked file was reported gone but survived"
    finally:
        parent.chmod(0o700)

    assert message is not None and "COULD NOT BE RESTORED" in message


def test_restore_returns_the_path_to_its_exact_pre_run_state_mtime_and_all(tmp_path):
    # `restore` reports the path is back to its pre-run state, and the fingerprint
    # the canary compares includes `st_mtime_ns`. A restore that put the bytes and
    # mode back but not the mtime left `snapshot(path) != before` — so the message
    # over-claimed. Restoring the mtime makes the claim literally true.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    path.chmod(0o640)
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    before = snapshot(path)

    path.write_bytes(b"leaked\n")  # the leak
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    assert restore(path, before) is True
    assert snapshot(path) == before, "restore left the path in a state the canary still flags"


@NEEDS_LUTIMES
def test_restore_returns_a_symlink_to_its_exact_pre_run_state(tmp_path):
    # Same claim, the symlink shape: a restored symlink is stamped back to its
    # pre-run mtime too (without following the link), so `snapshot(path) == before`
    # holds for it as well.
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    path = tmp_path / "app.json"
    path.symlink_to(target)
    os.utime(path, (1_600_000_000, 1_600_000_000), follow_symlinks=False)
    before = snapshot(path)

    path.unlink()
    path.symlink_to(target)  # the leak: same target, fresh mtime
    os.utime(path, (1_700_000_000, 1_700_000_000), follow_symlinks=False)

    assert restore(path, before) is True
    assert snapshot(path) == before, "the symlink's mtime was not put back"


def test_canary_refuses_to_destroy_what_it_cannot_reconstruct(tmp_path):
    # If the *pre-run* entry was something we could not read (here: a directory),
    # we never learned its contents. Removing it to "restore" would destroy the
    # very thing the canary exists to protect, so it must refuse and say so.
    path = tmp_path / "app.json"
    path.mkdir()
    (path / "keep.txt").write_text("precious", encoding="utf-8")
    before = snapshot(path)

    assert before.kind == "directory"
    restored = restore(path, before)

    assert restored is False
    assert (path / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_canary_reports_a_pre_run_entry_it_cannot_put_back(tmp_path):
    # Same category, but now the leak *is* visible (the directory was replaced by
    # a file): the canary must not claim it restored anything it could not.
    path = tmp_path / "app.json"
    path.mkdir()
    before = snapshot(path)

    assert before.kind == "directory"
    path.rmdir()
    path.write_bytes(b"{}\n")  # the leak
    message = leak_report(path, before)

    assert message is not None and "COULD NOT BE RESTORED" in message


def test_canary_reports_a_file_added_inside_a_pre_run_directory(tmp_path):
    # The hole this closes: pre-run *and* post-run the path is a directory, so a
    # kind-only comparison read `after == before` and the canary said nothing. A
    # shallow listing fingerprint makes the unknown-to-unknown change visible.
    path = tmp_path / "app.json"
    path.mkdir()
    (path / "keep.txt").write_text("precious", encoding="utf-8")
    before = snapshot(path)

    (path / "leaked.json").write_bytes(b"{}\n")  # the leak: written *inside* the directory
    message = leak_report(path, before)

    assert message is not None, "a write inside a pre-run directory went unreported"
    assert "modified" in message
    assert "COULD NOT BE RESTORED" in message and "directory" in message
    # Reported, never repaired: detection must not buy itself a destructive restore.
    assert (path / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert (path / "leaked.json").exists()


def test_canary_reports_a_rewritten_file_inside_a_pre_run_directory(tmp_path):
    path = tmp_path / "app.json"
    path.mkdir()
    child = path / "app.json"
    child.write_bytes(b'{"active_root": "/real/kb"}\n')
    before = snapshot(path)

    child.write_bytes(b'{"active_root": "/tmp/pytest-kb"}\n')  # the leak
    message = leak_report(path, before)

    assert message is not None, "a rewrite inside a pre-run directory went unreported"
    assert "COULD NOT BE RESTORED" in message


def test_canary_reports_a_same_size_rewrite_inside_a_pre_run_directory(tmp_path):
    # Same name, same size, same mode — only the timestamp moves. `os.utime` sets it
    # explicitly instead of racing the clock, so this does not depend on the
    # filesystem's mtime resolution.
    path = tmp_path / "app.json"
    path.mkdir()
    child = path / "app.json"
    child.write_bytes(b"aaaa")
    before = snapshot(path)

    child.write_bytes(b"bbbb")  # the leak: identical length
    os.utime(child, (1_600_000_000, 1_600_000_000))

    assert child.stat().st_size == 4
    message = leak_report(path, before)

    assert message is not None, "a same-size rewrite inside a pre-run directory went unreported"


def test_canary_reports_a_rewrite_two_levels_inside_a_pre_run_directory(tmp_path):
    # A shallow listing (direct children only) missed this: rewriting a file inside
    # a *sub*directory moves no ancestor's mtime, so `app.json/sub/app.json` — the
    # exact shape of a leaked KB config — compared clean. The fingerprint reaches
    # `_DIR_MAX_DEPTH` levels down for that reason.
    path = tmp_path / "app.json"
    (path / "sub").mkdir(parents=True)
    child = path / "sub" / "app.json"
    child.write_bytes(b'{"active_root": "/real/kb"}\n')
    before = snapshot(path)

    child.write_bytes(b'{"active_root": "/tmp/pytest-kb"}\n')  # the leak
    message = leak_report(path, before)

    assert message is not None, "a rewrite one level down in a pre-run directory went unreported"
    assert "COULD NOT BE RESTORED" in message


def test_canary_reports_a_same_size_rewrite_two_levels_inside_a_pre_run_directory(tmp_path):
    path = tmp_path / "app.json"
    (path / "sub").mkdir(parents=True)
    child = path / "sub" / "x.json"
    child.write_bytes(b"aaaa")
    before = snapshot(path)

    child.write_bytes(b"bbbb")  # the leak: identical length, only the timestamp moves
    os.utime(child, (1_600_000_000, 1_600_000_000))

    assert leak_report(path, before) is not None


def test_canary_reports_a_size_only_change_to_a_child_of_a_pre_run_directory(tmp_path):
    # Only `st_size` moves: same name, same mode, mtime pinned back by hand. Without
    # the size component in the listing digest this is invisible.
    path = tmp_path / "app.json"
    path.mkdir()
    child = path / "app.json"
    child.write_bytes(b"aaaa")
    dir_st = path.stat()
    child_st = child.stat()
    before = snapshot(path)

    child.write_bytes(b"aaaaaaaa")  # the leak: bigger, otherwise identical
    os.utime(child, ns=(child_st.st_atime_ns, child_st.st_mtime_ns))
    os.utime(path, ns=(dir_st.st_atime_ns, dir_st.st_mtime_ns))

    assert child.stat().st_mtime_ns == child_st.st_mtime_ns
    assert leak_report(path, before) is not None, "a size-only child change went unreported"


def test_the_directory_fingerprint_stops_at_a_documented_depth(tmp_path):
    # The honest edge of the bounded walk: a rewrite at depth 3 is *not* detected,
    # because no entry at depth ≤ 2 moves. Pinned so the docstring cannot drift from
    # the implementation and so widening the bound is a deliberate, visible change.
    path = tmp_path / "app.json"
    deep = path / "a" / "b"
    deep.mkdir(parents=True)
    child = deep / "c.json"
    child.write_bytes(b"aaaa")
    before = snapshot(path)

    child.write_bytes(b"bbbb")  # a depth-3 rewrite

    assert env_sandbox._DIR_MAX_DEPTH == 2
    assert leak_report(path, before) is None, (
        "the depth bound moved; update `_dir_fingerprint`'s docstring and this test together"
    )


def test_the_directory_listing_cannot_be_forged_by_a_child_name(tmp_path):
    # Separator forgery, the #174 bug in miniature. The old digest joined children
    # into one string with `,` and `:`, so a *single* child named
    # `a:0o100644:0:<mtime>,b` rendered byte-for-byte identically to *two* children
    # named `a` and `b` — different directories, same digest. The listing is a
    # structured tuple now, so the punctuation in a name is just punctuation.
    #
    # Asserted on the listing, not on the whole fingerprint: the fingerprint's head
    # carries the directory's own stat, which differs between any two directories
    # anyway and would make this pass without proving anything.
    stamp_ns = 1_600_000_000_000_000_000
    one = tmp_path / "one"
    one.mkdir()
    forged = one / f"a:0o100644:0:{stamp_ns},b"
    forged.write_bytes(b"")
    two = tmp_path / "two"
    two.mkdir()
    (two / "a").write_bytes(b"")
    (two / "b").write_bytes(b"")
    for child in (forged, two / "a", two / "b"):
        child.chmod(0o644)
        os.utime(child, ns=(stamp_ns, stamp_ns))

    assert env_sandbox._listing(one, 1) != env_sandbox._listing(two, 1), (
        "a child name forged the listing of a different directory"
    )


@NEEDS_FIFOS
def test_snapshot_splits_the_unreconstructable_kinds(tmp_path):
    # `other` used to cover all of these at once, which is what let unknown-to-unknown
    # changes compare clean. Each now names itself and carries a fingerprint.
    directory = tmp_path / "dir"
    directory.mkdir()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    assert snapshot(directory).kind == "directory"
    assert snapshot(directory).fingerprint is not None
    assert snapshot(fifo).kind == "unknown"
    assert snapshot(fifo).fingerprint is not None


@NOT_ROOT
def test_snapshot_marks_an_unreadable_file_and_refuses_to_restore_it(tmp_path):
    path = tmp_path / "app.json"
    path.write_bytes(b"secret\n")
    path.chmod(0o000)

    before = snapshot(path)

    assert before.kind == "unreadable_file"
    assert before.data is None and before.fingerprint is not None
    # Unreadable is not reconstructable, so restore leaves it alone rather than
    # deleting the user's file in the name of "restoring" it.
    assert restore(path, before) is False
    assert path.exists()


@NOT_ROOT
def test_a_pre_run_file_behind_an_unreadable_parent_is_never_deleted(tmp_path):
    # The blocker this closes. `lstat` failing with EACCES means "we could not
    # look", not "there is nothing here" — but it used to fold into `MISSING`,
    # which *is* restorable, and restoring `missing` means `_remove(path)`. So a
    # config dir that happened to be unsearchable at import time, and searchable
    # again by session end, got the user's real `app.json` deleted while the canary
    # said it had been "restored to its pre-run state".
    parent = tmp_path / "verinote"
    parent.mkdir()
    path = parent / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    parent.chmod(0o000)  # the pre-run state: present, but not even lstat-able

    try:
        before = snapshot(path)

        assert before.kind == "unknown", "an entry we failed to read must not read as `missing`"
        assert before.fingerprint == "lstat_failed:13"
        assert restore(path, before) is False, "an unread entry reached the destructive path"

        parent.chmod(0o700)  # the permission comes back mid-run; the file was there all along
        message = leak_report(path, before)
    finally:
        parent.chmod(0o700)

    assert path.read_bytes() == original, "the canary deleted the real config it could not read"
    assert message is not None and "COULD NOT BE RESTORED" in message


@NOT_ROOT
def test_a_pre_run_directory_behind_an_unreadable_parent_is_never_removed(tmp_path):
    # Same fault, worse blast radius: the old `_remove` would `rmtree` it.
    parent = tmp_path / "verinote"
    parent.mkdir()
    path = parent / "app.json"
    path.mkdir()
    (path / "keep.txt").write_text("precious", encoding="utf-8")
    parent.chmod(0o000)

    try:
        before = snapshot(path)
        parent.chmod(0o700)
        message = leak_report(path, before)
    finally:
        parent.chmod(0o700)

    assert before.kind == "unknown"
    assert (path / "keep.txt").read_text(encoding="utf-8") == "precious", "the canary rmtree'd it"
    assert message is not None and "COULD NOT BE RESTORED" in message


@NOT_ROOT
def test_canary_reports_a_size_only_rewrite_of_a_pre_run_unreadable_file(tmp_path):
    # Write-only mode: `snapshot` cannot read the bytes, so the fingerprint is all
    # the canary has. Only `st_size` moves here — mode and mtime are pinned — so
    # dropping `st_size` from the fingerprint makes this leak invisible.
    path = tmp_path / "app.json"
    path.write_bytes(b"aaaa")
    path.chmod(0o200)
    before = snapshot(path)
    st = path.lstat()

    assert before.kind == "unreadable_file"

    path.write_bytes(b"aaaaaaaa")  # the leak
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert path.lstat().st_mode == st.st_mode
    assert path.lstat().st_mtime_ns == st.st_mtime_ns
    message = leak_report(path, before)

    assert message is not None, "a size-only rewrite of an unreadable pre-run file went unreported"
    assert "COULD NOT BE RESTORED" in message
    assert path.lstat().st_size == 8, "an unreadable file must not be repaired, only reported"


@NEEDS_FIFOS
def test_canary_reports_a_chmod_of_a_pre_run_unknown_entry(tmp_path):
    # Only `st_mode` moves: a FIFO's size stays 0 and `chmod` does not touch mtime
    # (it moves ctime, which the fingerprint deliberately ignores). Dropping
    # `st_mode` makes this leak invisible.
    fifo = tmp_path / "app.json"
    os.mkfifo(fifo, 0o600)
    before = snapshot(fifo)
    st = fifo.lstat()

    assert before.kind == "unknown"

    fifo.chmod(0o644)  # the leak
    os.utime(fifo, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert fifo.lstat().st_size == st.st_size
    assert fifo.lstat().st_mtime_ns == st.st_mtime_ns
    message = leak_report(fifo, before)

    assert message is not None, "a chmod of a pre-run unknown entry went unreported"
    assert "COULD NOT BE RESTORED" in message


def test_canary_reports_a_chmod_of_the_real_config_and_restores_its_mode(tmp_path):
    # Widening the real `app.json` to 0o777 is a leak even though every byte is
    # unchanged — and `restore` must not itself re-permission the file: `mkstemp`
    # creates at 0o600, so writing the bytes back without a `chmod` silently
    # narrowed whatever the user had.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    path.chmod(0o640)
    before = snapshot(path)
    st = path.stat()

    assert before.kind == "file" and before.fingerprint is not None

    path.chmod(0o777)  # the leak: same bytes, wider permissions
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    message = leak_report(path, before)

    assert message is not None, "a chmod of the real config went unreported"
    assert "COULD NOT BE RESTORED" not in message
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640, "restore left the leaked permissions in place"


@pytest.mark.parametrize("kind", ["directory", "unreadable_file", "unknown"])
def test_restore_refuses_every_kind_it_could_not_read(tmp_path, kind):
    # The whitelist stated as behaviour rather than restated as a constant: a kind
    # outside it is refused *and* the path is left exactly as it was, which is the
    # property that matters (`restore` of a restorable kind starts with `_remove`).
    path = tmp_path / "app.json"
    path.write_bytes(b"precious\n")
    before = Entry(kind, fingerprint="anything")

    assert kind not in env_sandbox.RESTORABLE_KINDS
    assert restore(path, before) is False
    assert path.read_bytes() == b"precious\n", "restore destroyed what it could not reconstruct"


def test_snapshot_never_raises_on_odd_paths(tmp_path):
    # A path under a file, or a missing parent, must read as "nothing there"
    # rather than exploding inside the canary.
    (tmp_path / "file").write_text("x", encoding="utf-8")

    assert snapshot(tmp_path / "file" / "app.json") == MISSING
    assert snapshot(tmp_path / "missing" / "app.json") == MISSING


def test_snapshot_distinguishes_a_symlink_from_the_file_it_points_at(tmp_path):
    # Reading through the path would make these two indistinguishable, and a leak
    # that swaps the config for a symlink would compare equal to no leak at all.
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    assert snapshot(target).kind == "file"
    assert snapshot(link).kind == "symlink"
    assert snapshot(link) != snapshot(target)


def test_restore_puts_the_bytes_back_on_a_platform_with_no_fchmod(tmp_path, monkeypatch):
    # `os.fchmod` is Unix-only; Windows — which `verinote.config` explicitly supports —
    # has no such attribute. Calling it unguarded raised `AttributeError` *after*
    # `restore` had already deleted the file it was restoring, so the user's real
    # `app.json` was removed, never rewritten, and `pytest_sessionfinish` died on the
    # way to saying so. The mode is incidental; the bytes are the point.
    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    path.chmod(0o640)
    before = snapshot(path)

    path.write_bytes(b'{"active_root": "/tmp/pytest-kb"}\n')  # the leak
    message = leak_report(path, before)

    assert path.exists(), "restore deleted the config and could not put it back"
    assert path.read_bytes() == original
    assert message is not None and "COULD NOT BE RESTORED" not in message
    # `os.chmod` is the fallback where there is no fd-based one, so the mode survives too.
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_chmod_that_fails_does_not_abort_an_otherwise_complete_restore(tmp_path, monkeypatch):
    # The other half of the same lesson: a chmod can fail on its own terms (EPERM),
    # and a restore that put every byte back must not be undone by it.
    def unpermitted(*args, **kwargs):
        raise OSError(errno.EPERM, "not permitted")

    monkeypatch.setattr(os, "fchmod", unpermitted)
    monkeypatch.setattr(os, "chmod", unpermitted)
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    path.write_bytes(b"leaked\n")

    assert restore(path, before) is True
    assert path.read_bytes() == original


def test_a_failure_on_the_way_to_the_replace_leaves_the_config_on_disk(tmp_path, monkeypatch):
    # The structural fix, stated as behaviour. `restore` used to `_remove(path)` and
    # *then* write, so every failure in the gap — ENOSPC, EACCES on a read-only
    # parent, a raising chmod — lost the file for good. The replacement is staged
    # beside the path and `os.replace`d onto it now, so a failure anywhere in here
    # leaves the path holding something rather than nothing.
    path = tmp_path / "app.json"
    path.write_bytes(b'{"active_root": "/real/kb"}\n')
    before = snapshot(path)
    leaked = b'{"active_root": "/tmp/pytest-kb"}\n'
    path.write_bytes(leaked)

    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "replace", no_space)

    with pytest.raises(OSError):
        restore(path, before)

    assert path.exists(), "a failed restore deleted the config it was restoring"
    assert path.read_bytes() == leaked, "the path must hold the old entry until it holds the new"
    assert [p.name for p in tmp_path.iterdir()] == ["app.json"], "the staged temp file leaked"


def test_leak_report_hands_the_bytes_back_when_the_restore_blows_up(tmp_path, monkeypatch):
    # And when the restore cannot complete, the canary must not die holding the only
    # copy of the file: it reports, and it parks the pre-run bytes where a human can
    # get at them.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)
    path.write_bytes(b"leaked\n")

    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "replace", no_space)
    message = leak_report(path, before)

    assert message is not None and "COULD NOT BE RESTORED" in message
    assert "no space left on device" in message, "the report must say what actually failed"
    parked = Path(message.split("parked in ")[1].split()[0])
    try:
        assert parked.read_bytes() == original, "the parked bytes are not the pre-run bytes"
    finally:
        parked.unlink(missing_ok=True)


@NOT_ROOT
def test_canary_reports_an_mtime_only_rewrite_of_a_pre_run_unreadable_file(tmp_path):
    # Write-only mode, so the bytes were never read and the fingerprint is all the
    # canary has. Same size, same mode: only `st_mtime_ns` moves. Drop it from
    # `_stat_fingerprint` and this rewrite of the user's real config is invisible.
    path = tmp_path / "app.json"
    path.write_bytes(b"aaaa")
    path.chmod(0o200)
    before = snapshot(path)
    st = path.lstat()

    assert before.kind == "unreadable_file"

    path.write_bytes(b"bbbb")  # the leak: identical length
    os.utime(path, (1_600_000_000, 1_600_000_000))

    assert path.lstat().st_size == st.st_size
    assert path.lstat().st_mode == st.st_mode
    assert path.lstat().st_mtime_ns != st.st_mtime_ns
    message = leak_report(path, before)

    assert message is not None, "an mtime-only rewrite of an unreadable pre-run file was silent"
    assert "COULD NOT BE RESTORED" in message


def test_canary_reports_a_chmod_of_a_child_of_a_pre_run_directory(tmp_path):
    # Only the *child's* `st_mode` moves: a chmod touches ctime, not mtime, and it
    # changes no size and no name, so the parent directory looks untouched. The
    # child's mode is in the listing digest for exactly this.
    path = tmp_path / "app.json"
    path.mkdir()
    child = path / "app.json"
    child.write_bytes(b'{"active_root": "/real/kb"}\n')
    child.chmod(0o600)
    dir_st = path.stat()
    child_st = child.stat()
    before = snapshot(path)

    child.chmod(0o777)  # the leak: same bytes, world-writable now
    os.utime(child, ns=(child_st.st_atime_ns, child_st.st_mtime_ns))
    os.utime(path, ns=(dir_st.st_atime_ns, dir_st.st_mtime_ns))

    assert child.stat().st_size == child_st.st_size
    assert child.stat().st_mtime_ns == child_st.st_mtime_ns
    assert path.stat().st_mtime_ns == dir_st.st_mtime_ns

    assert leak_report(path, before) is not None, "a chmod of a child went unreported"


def test_canary_reports_a_chmod_of_a_pre_run_directory_itself(tmp_path):
    # Not the children — the directory. Its listing is unchanged (it is empty), its
    # size and mtime are pinned, so the only thing that moves is its own `st_mode`.
    # That is why the directory's own `lstat` is in the fingerprint's payload
    # alongside the listing.
    path = tmp_path / "app.json"
    path.mkdir(mode=0o755)
    before = snapshot(path)
    st = path.lstat()

    assert before.kind == "directory"

    path.chmod(0o700)  # the leak
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert path.lstat().st_mtime_ns == st.st_mtime_ns
    assert env_sandbox._listing(path, 1) == ()
    message = leak_report(path, before)

    assert message is not None, "a chmod of the pre-run directory itself went unreported"
    assert "COULD NOT BE RESTORED" in message


@NEEDS_LUTIMES
def test_canary_reports_a_symlink_re_created_with_the_same_target(tmp_path):
    # A symlink carries a fingerprint too, not just its target string. Without it, a
    # symlink swapped for a *different* symlink to the same place compares equal to no
    # change at all — and the point of the fingerprint is that a kind-preserving change
    # is still a change.
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    path = tmp_path / "app.json"
    path.symlink_to(target)
    before = snapshot(path)
    st = os.lstat(path)

    assert before.kind == "symlink" and before.fingerprint is not None

    path.unlink()
    path.symlink_to(target)  # the leak: same target, a different link
    os.utime(path, (1_600_000_000, 1_600_000_000), follow_symlinks=False)

    after = snapshot(path)

    assert after.target == before.target, "the targets must match, or this proves nothing"
    assert after.fingerprint != before.fingerprint
    assert os.lstat(path).st_mtime_ns != st.st_mtime_ns
    message = leak_report(path, before)

    assert message is not None, "only the symlink's fingerprint could have caught this"
    assert path.is_symlink() and os.readlink(path) == str(target)


@NOT_ROOT
def test_the_listing_records_why_it_could_not_read_a_directory(tmp_path):
    # A directory that cannot be listed is not an empty directory, and the two must not
    # digest the same. The errno says which one it is.
    locked = tmp_path / "locked"
    locked.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    locked.chmod(0o000)

    try:
        listing = env_sandbox._listing(locked, 1)
    finally:
        locked.chmod(0o700)

    assert listing == (("<listing-failed>", errno.EACCES),)
    assert listing != env_sandbox._listing(empty, 1)


def test_the_directory_fingerprint_sees_a_child_past_its_memory_bound(tmp_path):
    # The width hole. The listing used to keep the first `_DIR_MAX_ENTRIES` names and
    # then append only a *count*, so an in-place edit of the 300th child of a 300-child
    # directory moved nothing the digest could see: the child is not in the tuple, the
    # count does not change, and editing a file moves no parent's mtime. Every child is
    # digested now — the ones past the bound into a rolling hash — so the bound costs
    # memory, not sight.
    path = tmp_path / "app.json"
    path.mkdir()
    for index in range(env_sandbox._DIR_MAX_ENTRIES + 4):
        (path / f"{index:04d}.json").write_bytes(b"aaaa")
    child = path / f"{env_sandbox._DIR_MAX_ENTRIES + 3:04d}.json"
    before = snapshot(path)

    assert sorted(os.listdir(path)).index(child.name) >= env_sandbox._DIR_MAX_ENTRIES, (
        "the child must sort past the bound, or this tests the wrong half of the listing"
    )

    child.write_bytes(b"bbbb")  # the leak: same size, deep in the tail of a wide directory
    os.utime(child, (1_600_000_000, 1_600_000_000))

    assert leak_report(path, before) is not None, "an edit past the width bound went unreported"


def test_the_directory_listing_keeps_a_bounded_tuple(tmp_path):
    # The bound is real, it just buys memory rather than blindness: however wide the
    # directory, the listing holds at most `_DIR_MAX_ENTRIES` entries plus the one
    # overflow digest that stands in for all the rest.
    path = tmp_path / "wide"
    path.mkdir()
    for index in range(env_sandbox._DIR_MAX_ENTRIES + 30):
        (path / f"{index:04d}.json").write_bytes(b"")

    listing = env_sandbox._listing(path, 1)

    assert len(listing) == env_sandbox._DIR_MAX_ENTRIES + 1
    assert listing[-1][0] == "<overflow>"
    assert listing[-1][1] == 30, "the overflow must account for every child past the bound"


def test_canary_clears_a_directory_the_run_dropped_over_a_pre_run_file(tmp_path):
    # The only `rmtree` left in the module, and until now the only path through it
    # that nothing covered: the pre-run entry was a *file* we read byte for byte, and
    # the run replaced it with a directory. `os.replace` cannot overwrite a directory,
    # so `_clear_the_way` has to remove it first — and it is safe to, precisely
    # because a directory here is the run's own litter and the user's bytes are in
    # hand. Make `_clear_the_way` a no-op and the restore below cannot happen.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    path.chmod(0o640)
    before = snapshot(path)

    assert before.kind == "file"

    path.unlink()
    (path / "nested").mkdir(parents=True)  # the leak: a non-empty directory in its place
    (path / "nested" / "junk.json").write_bytes(b"{}\n")
    message = leak_report(path, before)

    assert path.is_file(), "the directory was left squatting where the config belongs"
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert message is not None and "COULD NOT BE RESTORED" not in message


def test_canary_never_rmtrees_the_directory_a_leaked_symlink_points_at(tmp_path, monkeypatch):
    # The same branch, with the leak shaped to weaponise it: the run swapped the
    # config for a symlink *into one of the user's directories*. `_clear_the_way`
    # looks with `lstat`, so it sees a symlink, not a directory, and clears nothing —
    # `os.replace` overwrites the link itself.
    #
    # Asserted on the call, not only on the survivors, and deliberately: with a `stat`
    # there, `rmtree` would be handed the symlink, and the user's tree would survive
    # only because `shutil` happens to refuse that with `ignore_errors=True` swallowing
    # the complaint. That is somebody else's invariant. What this module promises is
    # that it never *aims* its one `rmtree` at a link.
    victim = tmp_path / "kb"
    victim.mkdir()
    (victim / "precious.sqlite").write_bytes(b"do not touch\n")
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    aimed_at = []
    real_rmtree = shutil.rmtree
    monkeypatch.setattr(
        env_sandbox.shutil,
        "rmtree",
        lambda target, **kwargs: (aimed_at.append(Path(target)), real_rmtree(target, **kwargs))[1],
    )

    path.unlink()
    path.symlink_to(victim)  # the leak: a link at the config path, pointing into the user's KB
    message = leak_report(path, before)

    assert aimed_at == [], f"the canary aimed rmtree at a symlink: {aimed_at}"
    assert (victim / "precious.sqlite").read_bytes() == b"do not touch\n"
    assert not path.is_symlink()
    assert path.read_bytes() == original
    assert message is not None and "COULD NOT BE RESTORED" not in message


def test_a_failed_restore_over_a_leaked_directory_parks_the_bytes_it_could_not_write(
    tmp_path, monkeypatch
):
    # The honest edge of `restore`, pinned so nobody can read its docstring as a
    # promise it does not make. Clearing a leaked directory *is* a delete before a
    # write, so a `_stage` that fails here leaves the path empty — and the only thing
    # standing between that and a lost config is `_park`. Delete the park and this
    # test says so.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    path.unlink()
    path.mkdir()  # the leak: a directory, so `_clear_the_way` must remove it first
    (path / "junk.json").write_bytes(b"{}\n")

    real_mkstemp = tempfile.mkstemp

    def no_space_in_the_config_dir(*args, **kwargs):
        # Only the *staging* mkstemp fails (it writes beside the config); the rescue
        # copy's own mkstemp must still work, or this would prove nothing about `_park`.
        if kwargs.get("dir") == str(tmp_path):
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(env_sandbox.tempfile, "mkstemp", no_space_in_the_config_dir)
    message = leak_report(path, before)  # must not raise

    assert message is not None and "COULD NOT BE RESTORED" in message
    assert not path.exists(), (
        "if this now holds something, `restore` grew a stronger guarantee — say so in "
        "its docstring, and keep the park anyway"
    )
    parked = Path(message.split("parked in ")[1].split()[0])
    try:
        assert parked.read_bytes() == original, "the canary lost the only copy of the config"
    finally:
        parked.unlink(missing_ok=True)


def test_leak_report_parks_the_bytes_when_the_restore_is_interrupted(tmp_path, monkeypatch):
    # Ctrl-C is not an `Exception`. It landed in the one window where the path is
    # already empty (a leaked directory cleared, the file not yet replaced), sailed
    # through an `except Exception` rescue net, and took the user's only copy of
    # `app.json` with it — `pytest_sessionfinish` printed "check that file by hand"
    # about a file that no longer existed and bytes nobody held.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    path.unlink()
    path.mkdir()  # the leak

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(env_sandbox.os, "replace", interrupted)

    # Caught explicitly rather than left to propagate: a `KeyboardInterrupt` out of
    # here does not *fail* a pytest run, it *ends* one — the session stops, this test
    # never reports, and the regression reads as an absence rather than a red.
    try:
        message = leak_report(path, before)
    except BaseException as exc:  # noqa: BLE001 - the escape *is* the bug under test
        pytest.fail(f"leak_report let {exc!r} escape, so `_park` never ran and the bytes are gone")

    assert message is not None and "COULD NOT BE RESTORED" in message
    parked = Path(message.split("parked in ")[1].split()[0])
    try:
        assert parked.read_bytes() == original, "the canary died holding the only copy"
    finally:
        parked.unlink(missing_ok=True)


def test_the_directory_listing_does_not_depend_on_the_order_the_filesystem_returns(
    tmp_path, monkeypatch
):
    # `os.listdir` gives no ordering guarantee; the digest must. Without the `sorted`,
    # the same untouched directory fingerprints differently from one call to the next
    # — and every difference the canary sees is a leak it reports and then tries to
    # *restore*, which is how a phantom leak turns into a real write. The overflow
    # split makes it worse: which children land in the tuple and which fold into the
    # rolling hash would depend on readdir order too.
    path = tmp_path / "app.json"
    path.mkdir()
    for index in range(env_sandbox._DIR_MAX_ENTRIES + 4):
        (path / f"{index:04d}.json").write_bytes(b"aaaa")

    ordered = env_sandbox._listing(path, 1)

    real_listdir = os.listdir
    monkeypatch.setattr(env_sandbox.os, "listdir", lambda p: sorted(real_listdir(p), reverse=True))
    names = os.listdir(path)

    assert names != sorted(names), "the readdir order is not being shuffled; this proves nothing"
    assert env_sandbox._listing(path, 1) == ordered, (
        "the listing follows readdir order; an untouched directory would fingerprint "
        "differently on every call and the canary would 'restore' a leak nobody made"
    )


def test_the_rescue_copy_is_parked_outside_anything_the_sandbox_will_delete(tmp_path, monkeypatch):
    # `_park` is the last line of defence, so it must not write into a directory the
    # sandbox itself is about to `rmtree`. `tempfile.gettempdir()` falls back to the
    # home directory on Windows when `TMP`/`TEMP` are unset — and the sandbox patches
    # the home to a temp one and deletes it in `pytest_unconfigure`. Resolving the
    # rescue directory at *import* time (ambient environment) is what keeps the bytes
    # out of reach of that, exactly as for `REAL_APP_CONFIG_DIR`.
    ambient = tempfile.gettempdir()
    doomed = tmp_path / "sandbox-home-that-gets-rmtreed"
    doomed.mkdir()
    monkeypatch.setattr(env_sandbox.tempfile, "gettempdir", lambda: str(doomed))

    assert env_sandbox._RESCUE_DIR == ambient, "the rescue dir is not the one pinned at import"

    parked = Path(
        env_sandbox._park(tmp_path / "app.json", Entry("file", data=b"THE-ONLY-COPY")).split(
            "parked in "
        )[1].split()[0]
    )

    try:
        assert parked.read_bytes() == b"THE-ONLY-COPY"
        assert doomed not in parked.parents, "the rescue copy landed in the sandbox's own temp home"
        assert str(parked.parent) == env_sandbox._RESCUE_DIR
    finally:
        parked.unlink(missing_ok=True)
