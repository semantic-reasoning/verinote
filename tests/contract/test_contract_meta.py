# SPDX-License-Identifier: MPL-2.0
"""Meta guards for the #241 contract harness itself — deliberately *not* marked
``contract`` so they run in the default suite and stay green.

They catch the ways the harness could rot into a no-op: an unregistered marker
(so ``-m contract`` silently selects nothing), missing or provenance-less replay
fixtures, or a contract module that stops declaring any contract-marked test (so
the opt-in run collects zero guards).

The last two spawn a real nested pytest to pin the session guard in
``conftest.py``: asking for contract tests and skipping every one must exit
non-zero, while a run that never asked must stay green.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import API_KEY_VAR, BASE_URL_VAR, GATE_VAR, MODEL_VAR, _config_for

CONTRACT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONTRACT_DIR.parent.parent
FIXTURES_DIR = CONTRACT_DIR.parent / "fixtures" / "contract"
PROVENANCE_KEYS = ("provider", "model", "captured_at")
# Each replay guard depends on a specific fixture; naming them (instead of
# globbing for "at least one") makes deleting any single one turn this red.
REQUIRED_FIXTURES = (
    "query_intent_acme_ceo.json",
    "extraction_acme_two_dates.json",
    "sync_all_chunks_failed.json",
)
CONTRACT_MODULES = (
    "test_query_intent_contract.py",
    "test_extraction_contract.py",
    "test_sync_rc_contract.py",
)
# A module whose contract tests are *all* gated, so an ungated run executes none
# of them. `test_a_run_that_never_asked_for_contract_tests_stays_green` needs
# that property to mean anything; see its docstring.
GATE_ONLY_MODULE = "test_sync_rc_contract.py"


def test_contract_marker_is_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    assert any(m.startswith("contract:") for m in markers), (
        "the `contract` marker is not registered in pyproject.toml; `-m contract` "
        "would select nothing and silently pass"
    )


@pytest.mark.parametrize("fixture_name", REQUIRED_FIXTURES)
def test_required_replay_fixture_exists_and_carries_provenance(fixture_name):
    assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"
    path = FIXTURES_DIR / fixture_name
    assert path.is_file(), f"missing required contract fixture: {fixture_name}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data, f"empty fixture: {fixture_name}"
    missing = [key for key in PROVENANCE_KEYS if not data.get(key)]
    assert not missing, f"{fixture_name} is missing provenance keys: {missing}"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_contract_meta_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _contract_test_names(module) -> list[str]:
    names = []
    for name, obj in vars(module).items():
        if not name.startswith("test_") or not callable(obj):
            continue
        marks = getattr(obj, "pytestmark", [])
        if any(getattr(mark, "name", None) == "contract" for mark in marks):
            names.append(name)
    return names


@pytest.mark.parametrize("module_name", CONTRACT_MODULES)
def test_each_contract_module_is_collectable_and_has_a_guard(module_name):
    path = CONTRACT_DIR / module_name
    assert path.is_file(), f"missing contract module: {module_name}"
    module = _load_module(path)
    guards = _contract_test_names(module)
    assert guards, f"{module_name} declares no @pytest.mark.contract test"


def _nested_pytest(*args: str, gate_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run pytest in a child process from the repo root with a known gate.

    The autouse sandbox chdir's every test off the repo and this suite may be
    launched with the gate already exported, so the CWD and every `VN_CONTRACT_*`
    variable are pinned explicitly here rather than inherited.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("VN_CONTRACT_")}
    env.update(gate_env or {})
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider", "-q"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_all_skipped_contract_selection_fails_the_session():
    """`-m contract` with no gate must not be a green no-op — from any path.

    The parent-path form is the one that regressed (issue #272): it is the
    natural opt-in spelling, and reporting "N skipped" with exit 0 is exactly
    the false green this harness exists to prevent.
    """
    result = _nested_pytest("-m", "contract")
    assert result.returncode != 0, (
        "`pytest -m contract` with the gate unset exited 0 while skipping every "
        f"guard — a false green.\n{result.stdout}\n{result.stderr}"
    )
    assert "no guard executed" in result.stdout, (
        f"the session failed without saying why:\n{result.stdout}\n{result.stderr}"
    )


def test_a_run_that_never_asked_for_contract_tests_stays_green():
    """The default suite spelling must be untouched: guards self-skip, exit 0.

    Pins the other side of the session guard — it keys off an explicit
    `-m contract`, so collecting a contract module with no mark expression (what
    `pytest tests` does) must still pass with every guard skipped.

    `GATE_ONLY_MODULE` is the subject because *all* its contract tests are gated,
    so an ungated run executes none of them: exactly the state a wrongly-scoped
    guard would turn red. A module holding a contract-marked test that runs
    without a gate could not detect that. The skip count is asserted against the
    module's own guards to keep that true if the module ever gains a test — and
    this file is not the subject, since re-running it from inside itself would
    recurse.
    """
    module = _load_module(CONTRACT_DIR / GATE_ONLY_MODULE)
    guards = _contract_test_names(module)
    result = _nested_pytest(f"tests/contract/{GATE_ONLY_MODULE}", "-rs")
    assert result.stdout.count("SKIPPED") == len(guards), (
        f"{GATE_ONLY_MODULE} no longer skips all {len(guards)} of its guards on an "
        "ungated run, so this test can no longer detect a wrongly-scoped session "
        f"guard; point it at a gate-only module.\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, (
        "a run with no `-m contract` was turned red by the skipped-run guard; "
        f"the default suite would break.\n{result.stdout}\n{result.stderr}"
    )


def test_gate_variables_survive_the_root_sandbox_strip():
    """The gate must not sit under the prefix the root sandbox deletes.

    `tests/conftest.py` drops every `VERINOTE_*` variable at session start, so a
    gate named that way is gone before any fixture can read it (issue #272).
    Naming is the whole mechanism here, hence the guard.
    """
    for var in (GATE_VAR, MODEL_VAR, BASE_URL_VAR, API_KEY_VAR):
        assert not var.startswith("VERINOTE_"), (
            f"{var} is under the `VERINOTE_*` prefix that tests/conftest.py strips; "
            "it would never reach a contract fixture"
        )


def test_documented_api_key_reaches_the_provider_config():
    """The documented openai invocation must actually deliver the key.

    Runs the README's own openai spelling with a synthetic key. Authentication is
    expected to fail (or the SDK to be absent) — what must *not* happen is the
    harness reporting the key as missing, which is what a stripped variable looks
    like. Also asserts the run was not simply skipped, so a stripped *gate*
    cannot make this pass vacuously.
    """
    result = _nested_pytest(
        f"tests/contract/{CONTRACT_MODULES[0]}",
        "-m",
        "contract",
        gate_env={"VN_CONTRACT_PROVIDER": "openai", "VN_CONTRACT_API_KEY": "synthetic-key"},
    )
    assert "no API key was provided" not in result.stdout, (
        "VN_CONTRACT_API_KEY did not reach the provider config; the documented "
        f"openai run cannot work.\n{result.stdout}\n{result.stderr}"
    )
    assert "no guard executed" not in result.stdout, (
        f"the gate itself did not survive to the fixture.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("provider", "gate_env", "field", "expected"),
    [
        ("ollama", {MODEL_VAR: "qwen3:8b"}, "model", "qwen3:8b"),
        ("ollama", {BASE_URL_VAR: "http://ollama.example:11434"}, "base_url", "http://ollama.example:11434"),
        ("ollama", {}, "base_url", "http://localhost:11434"),
        ("openai", {API_KEY_VAR: "synthetic-key"}, "api_key", "synthetic-key"),
        ("openai", {MODEL_VAR: "gpt-4o-mini"}, "model", "gpt-4o-mini"),
        ("anthropic", {}, "model", "claude-opus-4-8"),
    ],
)
def test_companion_settings_map_onto_the_config(monkeypatch, tmp_path, provider, gate_env, field, expected):
    """Every companion variable the README documents must land on the Config.

    Complements the subprocess guard above: that one proves the variables survive
    the sandbox, this one proves each is wired to the field it claims, including
    the documented defaults.
    """
    for var in (MODEL_VAR, BASE_URL_VAR, API_KEY_VAR):
        monkeypatch.delenv(var, raising=False)
    for var, value in gate_env.items():
        monkeypatch.setenv(var, value)
    cfg = _config_for(provider, tmp_path / "kb")
    assert getattr(cfg, field) == expected
