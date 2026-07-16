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

from .conftest import API_KEY_VAR, BASE_URL_VAR, GATE_VAR, MODEL_VAR, _config_for, arms_skip_guard

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
# of them.
GATE_ONLY_MODULE = "test_sync_rc_contract.py"
# A module holding both a contract guard and an ungated control, so a run that
# deselects the guards still has something to execute.
MIXED_MODULE = "test_query_intent_contract.py"
# A keyword matching exactly one ungated control in this directory and no guard.
CONTROL_ONLY_KEYWORD = "deterministic_parser"


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


def test_targeting_the_contract_directory_fails_when_no_guard_runs():
    """`pytest tests/contract` with no gate must not be a green no-op either.

    The marker is not the only way to ask for these guards: naming the directory
    is the spelling a developer reaches for first. Left unarmed it reports
    "18 passed, 7 skipped" and exits 0 — the passing meta tests make it look
    especially green — while not one guard ran.

    This module is deselected in the child run: it is what spawns the child, so
    running it there would recurse. The directory stays the positional argument,
    which is what arms the guard. (`--deselect`, not `--ignore`: the latter is
    silently a no-op for a module inside a package like this one.)
    """
    result = _nested_pytest("tests/contract", f"--deselect=tests/contract/{Path(__file__).name}")
    assert result.returncode != 0, (
        "`pytest tests/contract` with the gate unset exited 0 while every guard "
        f"skipped — a false green.\n{result.stdout}\n{result.stderr}"
    )
    assert "no guard executed" in result.stdout, (
        f"the session failed without saying why:\n{result.stdout}\n{result.stderr}"
    )


def test_selecting_the_guards_by_keyword_fails_when_none_run():
    """`-k contract` with no gate must not be a green no-op either.

    This directory is a package named `contract`, so `-k contract` selects
    everything under it and the guards skip inside an otherwise-passing run.
    This module is deselected in the child to avoid recursing into itself.
    """
    result = _nested_pytest("-k", "contract", f"--deselect=tests/contract/{Path(__file__).name}")
    assert result.returncode != 0, (
        "`pytest -k contract` with the gate unset exited 0 while every guard "
        f"skipped — a false green.\n{result.stdout}\n{result.stderr}"
    )
    assert "no guard executed" in result.stdout, (
        f"the session failed without saying why:\n{result.stdout}\n{result.stderr}"
    )


def test_filtering_the_guards_out_by_keyword_is_not_a_failure():
    """`-k` that excludes the guards is not "they never ran".

    The mirror of the test above, and the boundary it must not cross: arming is
    not failing. The run targets this directory (so the guard *is* armed) but
    the keyword leaves zero guards selected — silence is the only correct
    outcome.

    `CONTROL_ONLY_KEYWORD` matches a single ungated control. The obvious `-k meta`
    would select this module, which spawns the child, and recurse.
    """
    result = _nested_pytest("tests/contract", "-k", CONTROL_ONLY_KEYWORD)
    assert result.returncode == 0, (
        "a run that filtered the contract tests out by keyword was failed for "
        f"not running them.\n{result.stdout}\n{result.stderr}"
    )
    assert "1 passed" in result.stdout, (
        f"{CONTROL_ONLY_KEYWORD!r} no longer selects exactly the one control this "
        f"test needs.\n{result.stdout}\n{result.stderr}"
    )


def test_deselecting_the_guards_is_not_a_failure():
    """`-m "not contract"` deselects the guards; that is not "they never ran".

    The count must be taken after deselection, or a run that deliberately
    excludes the guards fails because the guards it excluded did not execute.

    `MIXED_MODULE` holds both a contract guard and an ungated control, so this
    child run has something left to execute after the guards are deselected —
    a module of guards only would exit 5 (nothing collected) and prove nothing.
    """
    result = _nested_pytest(f"tests/contract/{MIXED_MODULE}", "-m", "not contract")
    assert result.returncode == 0, (
        "a run that deselected the contract tests was failed for not running "
        f"them.\n{result.stdout}\n{result.stderr}"
    )


def test_collect_only_is_not_failed_by_the_skip_guard():
    """Collecting is not running, so an empty run is the correct outcome.

    Failing `--collect-only` would be the mirror of the bug this guard fixes: a
    red run that had nothing to report.
    """
    result = _nested_pytest("-m", "contract", "--collect-only")
    assert result.returncode == 0, (
        "`--collect-only` was failed by the skipped-run guard; it never intended "
        f"to run anything.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("markexpr", "keyword", "args", "arms", "why"),
    [
        ("contract", "", ["tests"], True, "the marker is named"),
        ("not contract", "", ["tests"], True, "conservative: mark expr mentions it; selection count decides"),
        ("", "contract", ["tests"], True, "the keyword is named"),
        ("", "contract", [], True, "the keyword is named, bare pytest"),
        ("", "not contract", ["tests"], True, "conservative: keyword mentions it; selection count decides"),
        ("", "", ["tests/contract"], True, "the directory is named"),
        ("", "", [str(CONTRACT_DIR)], True, "the directory is named absolutely"),
        ("", "", [f"tests/contract/{GATE_ONLY_MODULE}"], True, "a module inside it is named"),
        ("", "", [f"tests/contract/{GATE_ONLY_MODULE}::test_x"], True, "a single test inside it is named"),
        ("", "", ["tests.contract"], True, "--pyargs names it as a dotted module"),
        ("", "", [f"tests.contract.{GATE_ONLY_MODULE.removesuffix('.py')}"], True, "--pyargs names a module inside"),
        ("", "", ["tests"], False, "the default suite: a parent, not a path inside"),
        ("", "", [], False, "bare pytest before testpaths expands"),
        ("", "meta", ["tests"], False, "an unrelated keyword"),
        ("", "", ["tests/test_config.py"], False, "an unrelated module"),
        ("", "", ["tests/contract_notes"], False, "a sibling whose name merely starts the same"),
        ("", "", ["tests.test_config"], False, "an unrelated dotted module"),
    ],
)
def test_skip_guard_arming_boundary(markexpr, keyword, args, arms, why):
    """Pin exactly which invocations arm the skipped-run guard.

    Every input pytest can express "I want the contract guards" with has to be an
    argument here: a spelling the function cannot see is a spelling it cannot
    guard. `-k` was the third false green found precisely because it was missing.

    The default-suite rows are the load-bearing ones: `pytest` and `pytest tests`
    both pass `tests`, a *parent* of this directory. If either armed, every
    default run would go red on the self-skipping guards.
    """
    assert arms_skip_guard(markexpr, keyword, args, REPO_ROOT) is arms, why


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
