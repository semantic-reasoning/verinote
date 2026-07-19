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

import ast
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from . import conftest as contract_conftest
from .conftest import (
    API_KEY_VAR,
    BASE_URL_VAR,
    GATE_VAR,
    MODEL_VAR,
    _client_for_provider,
    _config_for,
    arms_skip_guard,
)

CONTRACT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONTRACT_DIR.parent.parent
FIXTURES_DIR = CONTRACT_DIR.parent / "fixtures" / "contract"
RUN_SH = CONTRACT_DIR / "run.sh"
# The wrapper needs a shell and `dirname`; everything else it uses is a builtin.
# Located via the ambient PATH here, then re-exposed on the *controlled* PATH the
# wrapper actually runs with, so locating them is not the thing under test.
WRAPPER_TOOLS = ("bash", "dirname")
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


def test_documented_api_key_reaches_the_provider_config(monkeypatch, tmp_path):
    """The documented openai invocation must actually deliver the key.

    This is a wiring assertion, not a live-provider contract. The default meta
    suite must never depend on the OpenAI SDK or network just to prove the
    documented environment spelling reaches Config, so `get_client` is replaced
    before the provider adapter boundary.
    """
    monkeypatch.setenv(GATE_VAR, "openai")
    monkeypatch.setenv(API_KEY_VAR, "synthetic-key")
    captured = {}

    def fake_get_client(cfg):
        captured["cfg"] = cfg
        return object()

    monkeypatch.setattr(contract_conftest, "get_client", fake_get_client)

    provider = contract_conftest.contract_provider()
    assert provider == "openai"

    client = _client_for_provider(provider, tmp_path / "kb")

    assert client is not None
    assert captured["cfg"].provider == "openai"
    assert captured["cfg"].api_key == "synthetic-key"


def test_meta_nested_pytest_never_opts_into_a_live_provider():
    """Default-suite meta tests must not spawn opted-in live contract runs."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    live_gate_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_nested_pytest":
            continue
        for keyword in node.keywords:
            if keyword.arg != "gate_env" or not isinstance(keyword.value, ast.Dict):
                continue
            keys = []
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant):
                    keys.append(key.value)
                elif isinstance(key, ast.Name):
                    keys.append(globals().get(key.id))
            if GATE_VAR in keys:
                live_gate_calls.append(node.lineno)
    assert live_gate_calls == []


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

    Complements the adapter-boundary guard above: that one proves the documented
    OpenAI key reaches the Config without a live call, this one proves each
    companion variable is wired to the field it claims, including the documented
    defaults.
    """
    for var in (MODEL_VAR, BASE_URL_VAR, API_KEY_VAR):
        monkeypatch.delenv(var, raising=False)
    for var, value in gate_env.items():
        monkeypatch.setenv(var, value)
    cfg = _config_for(provider, tmp_path / "kb")
    assert getattr(cfg, field) == expected


# --- the documented wrapper actually reaches pytest -----------------------
#
# The guards above spawn pytest with `sys.executable`, which is exactly why they
# could not catch issue #273: they bypass `run.sh` entirely, so the wrapper could
# be broken while every one of them stayed green. These run the wrapper itself.


def _shim_dir(tmp_path: Path, interpreters: tuple[str, ...]) -> Path:
    """A PATH directory exposing *only* `interpreters` as Python, plus the shell.

    The whole point is to not depend on the ambient PATH. This machine happens to
    have `python3` and no `python`, but a machine that has both (CI included)
    would let a wrapper defaulting to `python` pass vacuously — the bug would
    survive precisely where it is not reproducible. Pinning PATH to this
    directory means each case holds everywhere.

    Each shim is a real executable that forwards to the interpreter running this
    test, so a wrapper that finds it gets a Python with pytest installed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in WRAPPER_TOOLS:
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"{tool} is not available; the wrapper cannot run on this platform")
        (bin_dir / tool).symlink_to(real)
    for name in interpreters:
        shim = bin_dir / name
        shim.write_text(f'#!{shutil.which("bash")}\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_wrapper(
    tmp_path: Path,
    *args: str,
    interpreters: tuple[str, ...],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Execute `run.sh` for real, with PATH pinned to `interpreters` only.

    Invoked as the script itself (not `bash run.sh`) so the shebang is exercised
    the way a user's shell would exercise it. The gate is left unset on purpose:
    a set gate would call a live provider, and this test is about the wrapper, not
    about any model. Unset means the guards skip and the session guard in
    conftest fails the run — which is the evidence that pytest was reached.
    """
    bin_dir = _shim_dir(tmp_path, interpreters)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VN_CONTRACT_")}
    env.pop("PYTHON", None)
    env["PATH"] = str(bin_dir)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra_env or {})
    return subprocess.run(
        [str(RUN_SH), *args, "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _assert_reached_pytest(result: subprocess.CompletedProcess, how: str) -> None:
    """Assert the wrapper got as far as running pytest.

    Deliberately *not* `exit 0`: with the gate unset every guard skips and the
    session guard fails the run on purpose, so zero would be wrong. The signal is
    that pytest ran and spoke — the guard's own message — and that the shell never
    failed to find an interpreter (127 / "not found"), which is how issue #273
    presented.
    """
    assert result.returncode != 127, (
        f"the wrapper never reached pytest ({how}); the shell could not exec its "
        f"interpreter.\n{result.stdout}\n{result.stderr}"
    )
    assert "not found" not in result.stderr, (
        f"the wrapper failed to resolve a command ({how}).\n{result.stdout}\n{result.stderr}"
    )
    assert "no guard executed" in result.stdout, (
        f"the wrapper did not reach pytest ({how}): the contract session guard "
        f"never spoke.\n{result.stdout}\n{result.stderr}"
    )


def test_wrapper_reaches_pytest_when_only_python3_exists(tmp_path):
    """The README's own command must work where `python` is not a binary.

    This is issue #273 as reported: modern distributions (and this machine) ship
    `python3` only, so a wrapper defaulting to `python` dies at
    `exec: python: not found` — exit 127, before pytest, on the very command the
    README tells people to run.
    """
    result = _run_wrapper(tmp_path, "-q", interpreters=("python3",))
    _assert_reached_pytest(result, "python3-only PATH")


def test_wrapper_reaches_pytest_when_only_python_exists(tmp_path):
    """...and must keep working where `python` is the only spelling.

    The mirror of the case above, and the reason the fix cannot simply be
    s/python/python3/: virtualenvs and older images expose `python` alone. A
    wrapper that hard-codes either name is broken on half the world.
    """
    result = _run_wrapper(tmp_path, "-q", interpreters=("python",))
    _assert_reached_pytest(result, "python-only PATH")


def test_wrapper_honours_an_explicit_python_override(tmp_path):
    """`PYTHON=... run.sh` must still win over whatever discovery finds.

    PATH here holds *no* interpreter at all, so the run can only reach pytest via
    the override — if it were ignored, discovery would have nothing to fall back
    on and this would go red rather than pass by luck.
    """
    result = _run_wrapper(
        tmp_path, "-q", interpreters=(), extra_env={"PYTHON": sys.executable}
    )
    _assert_reached_pytest(result, "explicit PYTHON override")


def test_wrapper_fails_loudly_when_no_interpreter_exists(tmp_path):
    """No Python anywhere is a diagnosis, not a stray shell error.

    The boundary of the discovery fix: when it genuinely cannot find an
    interpreter the wrapper must say so and exit non-zero, rather than emit
    bash's own `command not found` and leave the user guessing which name it
    wanted.
    """
    result = _run_wrapper(tmp_path, "-q", interpreters=())
    assert result.returncode != 0, (
        f"the wrapper found no interpreter yet exited 0.\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "PYTHON" in combined, (
        "the wrapper gave no hint that PYTHON can point it at an interpreter.\n"
        f"{result.stdout}\n{result.stderr}"
    )
