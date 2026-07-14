# SPDX-License-Identifier: MPL-2.0
"""Tests for the test harness itself: the sandbox must contain a careless test.

The bug these guard against is not hypothetical. `POST /settings/root` calls
`save_active_root()`, which writes the platform app config resolved from `HOME`
(and friends); a web test that forgot to isolate `HOME` therefore rewrote the
developer's *real* `app.json` and repointed their active KB at a temp directory
that pytest then deleted. `active_root()` also falls back to `./data` relative
to the CWD, so an unisolated test run from the repo root opens the repo's own
`data/kb.sqlite`.

Every test below deliberately omits manual isolation and never requests the
`isolate_app_environment` fixture by name — it must be the *autouse* fixture in
`conftest.py` (or the `pytest_configure` seal beneath it) doing the work, so
that deleting either turns these red.
"""

import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import conftest  # noqa: E402
import env_sandbox  # noqa: E402
from env_sandbox import (  # noqa: E402
    MISSING,
    REAL_APP_CONFIG_DIR,
    REAL_APP_CONFIG_PATH,
    Entry,
    leak_report,
    restore,
    sandbox_home,
    session_home,
    snapshot,
)
from verinote.config import Config, active_root, app_config_dir, app_config_path  # noqa: E402
from verinote.web import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

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


@pytest.fixture(scope="module")
def module_scoped_app_config_dir():
    # Higher-scoped fixtures are instantiated *before* function-scoped ones, so
    # this resolves the app config dir before the per-test `monkeypatch` sandbox
    # exists. Only the session-start seal can keep it off the real home.
    return app_config_dir()


@pytest.fixture(scope="module")
def module_scoped_config():
    # The realistic version of the hole: a module-scoped fixture built for speed
    # that loads config once. Without the seal this reads the developer's real
    # `app.json` and can pick their real KB.
    return Config.load()


def test_pytest_cannot_reach_the_real_app_config():
    home = sandbox_home()

    assert home is not None, "the autouse environment sandbox is not installed"
    assert app_config_dir() != REAL_APP_CONFIG_DIR
    assert home in app_config_path().parents


def test_settings_switch_without_manual_isolation_stays_in_the_sandbox(tmp_path):
    # No monkeypatch.setenv("HOME", ...) here — that is the whole point: this is
    # the test an author would write after forgetting to isolate.
    before = snapshot(REAL_APP_CONFIG_PATH)
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))
    other = tmp_path / "other-kb"

    r = client.post("/settings/root", data={"root": str(other)}, follow_redirects=False)

    assert r.status_code == 303
    assert snapshot(REAL_APP_CONFIG_PATH) == before, (
        f"the KB switch escaped the sandbox and wrote {REAL_APP_CONFIG_PATH}"
    )
    home = sandbox_home()
    assert home is not None
    assert home in app_config_path().parents
    assert app_config_path().is_file()
    assert active_root() == other.resolve()


def test_active_root_fallback_is_cwd_relative_and_the_sandbox_cwd_is_off_the_repo(
    tmp_path, monkeypatch
):
    # Prove the CWD guard without relying on the repo's own `data/` (which is
    # gitignored, so it does not exist on CI — asserting `active_root() is None`
    # there would pass even with the chdir isolation deleted). Instead: plant a
    # KB under a temp CWD and show the fallback follows the CWD, then show the
    # sandbox CWD has no KB and is not the repo.
    planted = tmp_path / "data"
    planted.mkdir()
    (planted / "kb.sqlite").write_text("", encoding="utf-8")

    with monkeypatch.context() as m:
        m.chdir(tmp_path)
        assert active_root() == planted.resolve(), "the `./data` fallback is CWD-relative"

    cwd = Path.cwd()

    assert cwd != REPO_ROOT, "the test run must not sit in the repo root"
    assert REPO_ROOT not in cwd.parents, "the test run must not sit inside the repo"
    assert active_root() is None, "the sandbox CWD must hold no KB to fall back to"


def test_module_scoped_fixtures_run_inside_the_sandbox(module_scoped_app_config_dir):
    # The reviewer's reproduction: a module-scoped fixture resolves before any
    # function-scoped monkeypatch, so a function-only sandbox let it read the
    # developer's real `~/Library/Application Support/verinote`.
    home = session_home()

    assert home is not None, "the session-start environment seal is not installed"
    assert module_scoped_app_config_dir != REAL_APP_CONFIG_DIR
    assert home in module_scoped_app_config_dir.parents


def test_module_scoped_config_load_stays_in_the_sandbox(module_scoped_config):
    # `Config.load()` resolves the root through the real `app.json` *and* the
    # CWD-relative `./data` fallback, so both tiers of the leak show up here.
    root = module_scoped_config.root

    assert session_home() is not None, "the session-start environment seal is not installed"
    assert root != REPO_ROOT / "data", "a module-scoped Config.load() reached the repo's KB"
    assert REPO_ROOT not in root.parents
    assert module_scoped_config.db_path == root / "kb.sqlite"


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


def test_the_real_config_baseline_is_captured_at_import_time():
    # The baseline must be a module-level constant, not fixture state: a fixture's
    # setup runs after collection and after every test module's import-time code,
    # so a leak from there would be baked into the baseline and read as clean.
    # Guard the *shape*, because that is what the bug was.
    assert isinstance(env_sandbox.REAL_APP_CONFIG_BEFORE, Entry)
    assert not hasattr(conftest, "real_app_config_is_untouched"), (
        "the canary regressed to a session fixture; its baseline would be taken too late"
    )
    assert hasattr(conftest, "pytest_sessionfinish"), (
        "the canary must run from a hook so it still fires when collection fails "
        "or no test is selected"
    )
