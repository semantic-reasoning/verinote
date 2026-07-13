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
`conftest.py` doing the work, so that deleting `autouse=True` turns these red.
"""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from env_sandbox import (  # noqa: E402
    REAL_APP_CONFIG_DIR,
    REAL_APP_CONFIG_PATH,
    sandbox_home,
    snapshot,
)
from verinote.config import Config, active_root, app_config_dir, app_config_path  # noqa: E402
from verinote.web import create_app  # noqa: E402


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


def test_config_load_does_not_fall_back_to_the_repo_data_kb():
    # No VERINOTE_ROOT, no app.json, and the CWD is off the repo — so
    # `active_root()`'s `./data` fallback must not resolve to the repo's own KB.
    repo_data = Path(__file__).resolve().parent.parent / "data"

    root = active_root()

    assert root != repo_data
    assert root is None
