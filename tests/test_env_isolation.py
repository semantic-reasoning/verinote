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

    assert before.kind == "other"
    restored = restore(path, before)

    assert restored is False
    assert (path / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_canary_reports_a_pre_run_entry_it_cannot_put_back(tmp_path):
    # Same category, but now the leak *is* visible (the directory was replaced by
    # a file): the canary must not claim it restored anything it could not.
    path = tmp_path / "app.json"
    path.mkdir()
    before = snapshot(path)

    assert before.kind == "other"
    path.rmdir()
    path.write_bytes(b"{}\n")  # the leak
    message = leak_report(path, before)

    assert message is not None and "COULD NOT BE RESTORED" in message


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
