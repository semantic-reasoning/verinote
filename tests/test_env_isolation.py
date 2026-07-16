# SPDX-License-Identifier: MPL-2.0
"""Tests that the harness itself isolates a careless test from the real environment.

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

The pure `env_sandbox` helper tests live in `test_env_sandbox_helpers.py`: they
need neither the sandbox fixtures nor FastAPI, so keeping them here behind the
FastAPI gate below would have skipped the harness's own self-tests on a machine
without FastAPI. Only the one test that drives the web stack is gated now.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
import env_sandbox
from env_sandbox import (
    REAL_APP_CONFIG_DIR,
    REAL_APP_CONFIG_PATH,
    Entry,
    sandbox_home,
    session_home,
    snapshot,
)
from verinote.config import Config, active_root, app_config_dir, app_config_path

# The web stack is the only thing here that needs FastAPI. Guarding just the one
# test that uses it — rather than `importorskip`ing the whole module — keeps the
# fixture-level isolation tests running on a machine without FastAPI.
try:
    from fastapi.testclient import TestClient

    from verinote.web import create_app

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only where FastAPI is absent
    HAS_FASTAPI = False

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


@pytest.mark.skipif(not HAS_FASTAPI, reason="this test drives the FastAPI web stack")
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


def test_the_session_hook_survives_a_canary_that_raises(monkeypatch, capsys):
    # `pytest_sessionfinish` called `leak_report` bare, so anything it raised became an
    # INTERNALERROR — the traceback replaced the one message telling the user their real
    # config needed looking at. Fail the run loudly instead of taking the session down.
    def broken(*args, **kwargs):
        raise AttributeError("module 'os' has no attribute 'fchmod'")

    monkeypatch.setattr(env_sandbox, "leak_report", broken)
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: None)),
        exitstatus=0,
    )

    conftest.pytest_sessionfinish(session, 0)  # must not raise

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "repair it by hand" in capsys.readouterr().out


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
