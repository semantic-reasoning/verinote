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

from env_sandbox import (  # noqa: E402
    REAL_APP_CONFIG_DIR,
    REAL_APP_CONFIG_PATH,
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
    # The canary's teardown does not just detect a leak; it undoes it. Exercised
    # here against a stand-in path — never the real one.
    path = tmp_path / "app.json"
    original = b'{"active_root": "/real/kb"}\n'
    path.write_bytes(original)
    before = snapshot(path)

    path.write_bytes(b'{"active_root": "/tmp/pytest-kb"}\n')  # the leak
    restore(path, before)

    assert path.read_bytes() == original


def test_canary_restore_removes_a_config_the_run_created(tmp_path):
    path = tmp_path / "nested" / "app.json"
    before = snapshot(path)

    assert before is None
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{}\n")  # the leak: a file that did not exist before
    restore(path, before)

    assert not path.exists()


def test_snapshot_never_raises_on_odd_paths(tmp_path):
    # A directory where app.json should be, or a path under a file, must read as
    # "nothing there" rather than exploding inside the session-scoped canary.
    (tmp_path / "app.json").mkdir()
    (tmp_path / "file").write_text("x", encoding="utf-8")

    assert snapshot(tmp_path / "app.json") is None
    assert snapshot(tmp_path / "file" / "app.json") is None
    assert snapshot(tmp_path / "missing" / "app.json") is None
