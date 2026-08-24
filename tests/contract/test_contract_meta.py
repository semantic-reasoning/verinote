# SPDX-License-Identifier: MPL-2.0
"""Meta guards for the #241 contract harness itself — deliberately *not* marked
``contract`` so they run in the default suite and stay green.

They catch ways the harness could rot into a no-op: an unregistered marker (so
``-m contract`` silently selects nothing), missing or provenance-less replay
fixtures, a module in ``CONTRACT_MODULES`` that stops declaring a
contract-marked test (so the opt-in run loses that module's guards, and collects
none at all if it was the last), a module that appears here on neither list, and
a guard the promotion ledger names slipping back out of the default suite.

The session guard in ``conftest.py`` is pinned by spawning a real nested pytest:
asking for contract tests and skipping every one must exit non-zero, while a run
that never asked must stay green.
"""

from __future__ import annotations

import ast
from datetime import date
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from verinote.config import Config

from . import conftest as contract_conftest
from . import capture
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
PROVENANCE_KEYS = ("provider", "model", "captured_at", "prompt_id", "input")
LIVE_FIXTURE_NAMES = {
    "query_intent_acme_ceo.json": ("query-intent", capture.QUERY_INTENT_QUESTION, (str, dict)),
    "extraction_acme_two_dates.json": ("extraction", capture.EXTRACTION_SOURCE, (str, list, dict)),
}
CONTRACT_MODULES = (
    "test_query_intent_contract.py",
    "test_extraction_contract.py",
    "test_openrouter_catalogue_contract.py",
)
# A module inside this directory, for the `test_skip_guard_arming_boundary`
# rows that need one named. Any path under this directory serves:
# `arms_skip_guard` resolves the argument and compares it against this directory
# without ever opening it, so the module an argument names need not exist — that
# is what the row spelling out a module that does not exist is there to pin. The
# name says no more than "inside the directory" because that is all those rows
# need; the constant it replaced claimed its module's tests were *all* gated, a
# property every promotion can take away and no code reads.
MODULE_INSIDE_DIR = "test_sync_rc_contract.py"
# A module holding both a contract guard and an ungated control, so a run that
# deselects the guards still has something to execute.
MIXED_MODULE = "test_query_intent_contract.py"
# A keyword matching exactly one ungated control in this directory and no guard.
CONTROL_ONLY_KEYWORD = "deterministic_parser"
REPLAY_ONLY_TARGETS = (
    "tests/contract/test_query_intent_contract.py::test_replay_raw_intent_parses_through_production_boundary",
    "tests/contract/test_query_intent_contract.py::test_claudecli_replay_retains_reason_regression_shape",
    "tests/contract/test_extraction_contract.py::test_replay_founding_relation_normalizes_into_functional_vocab",
)
# Guards a promotion moved into the default suite by taking away their marker
# and their gate, spelled as (module, function) so the promotion can be asserted
# at the source level. Listed rather than discovered: a promoted guard that is
# simply deleted must be as visible as one that is re-gated.
PROMOTED_GUARDS = {
    "test_extraction_contract.py": (
        "test_replay_founding_relation_normalizes_into_functional_vocab",
        "test_functional_conflict_fires_on_two_dates",
    ),
    "test_query_intent_contract.py": (
        "test_replay_raw_intent_parses_through_production_boundary",
        "test_claudecli_replay_retains_reason_regression_shape",
    ),
    "test_sync_rc_contract.py": ("test_sync_fails_when_every_chunk_fails",),
}
# The promoted guards `REPLAY_ONLY_TARGETS` does not already name, matched on
# the function name, as node ids. Derived from the ledger above rather than
# listed again, so a promotion recorded there cannot be left out of the run
# below by forgetting a second list.
_REPLAY_NAMES = frozenset(target.rsplit("::", 1)[1] for target in REPLAY_ONLY_TARGETS)
DETERMINISTIC_PROMOTED_TARGETS = tuple(
    f"tests/contract/{module_name}::{name}"
    for module_name in sorted(PROMOTED_GUARDS)
    for name in PROMOTED_GUARDS[module_name]
    if name not in _REPLAY_NAMES
)


def test_contract_marker_is_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    assert any(m.startswith("contract:") for m in markers), (
        "the `contract` marker is not registered in pyproject.toml; `-m contract` "
        "would select nothing and silently pass"
    )


def test_sync_replay_fixture_exists_and_carries_provenance():
    assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"
    path = FIXTURES_DIR / "sync_all_chunks_failed.json"
    assert path.is_file(), f"missing required contract fixture: {path.name}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data, f"empty fixture: {path.name}"
    missing = [key for key in PROVENANCE_KEYS if not data.get(key)]
    assert not missing, f"{path.name} is missing provenance keys: {missing}"
    assert data["input"] == capture.EXTRACTION_SOURCE


def _valid_live_fixture_pairs() -> dict[str, set[str]]:
    """Validate live fixture metadata and return prompt pairs by provider."""
    paths = tuple(sorted(FIXTURES_DIR.glob("*/*.json")))
    assert paths, f"no provider-qualified live fixtures under {FIXTURES_DIR}"
    pairs: dict[str, set[str]] = {}
    for path in paths:
        assert path.name in LIVE_FIXTURE_NAMES, (
            f"unexpected live fixture: {path.relative_to(FIXTURES_DIR)}"
        )
        expected_prompt, expected_input, raw_types = LIVE_FIXTURE_NAMES[path.name]
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in PROVENANCE_KEYS if not data.get(key)]
        assert not missing, (
            f"{path.relative_to(FIXTURES_DIR)} is missing provenance keys: {missing}"
        )
        assert path.parent.name in capture._ADAPTER_MODULES, (
            f"unknown fixture provider path: {path.parent.name}"
        )
        assert data["provider"] == path.parent.name, (
            f"fixture provider does not match its path: {path.relative_to(FIXTURES_DIR)}"
        )
        assert data["prompt_id"] == expected_prompt
        assert data["input"] == expected_input, (
            "live fixture input must be the synthetic capture input"
        )
        assert isinstance(data["model"], str) and data["model"]
        date.fromisoformat(data["captured_at"])
        assert isinstance(data.get("raw_response"), raw_types), (
            f"{path.relative_to(FIXTURES_DIR)} has an unsupported raw response type"
        )
        pairs.setdefault(path.parent.name, set()).add(data["prompt_id"])
    assert all(prompts == {"query-intent", "extraction"} for prompts in pairs.values()), (
        "each provider fixture directory must contain a query-intent/extraction pair"
    )
    return pairs


def test_live_replay_fixtures_have_valid_provider_qualified_metadata():
    """Every live replay is a paired, synthetic capture in its provider directory."""
    _valid_live_fixture_pairs()


def test_legacy_flat_live_fixtures_are_rejected():
    """The deterministic sync artifact is the only allowed flat contract fixture."""
    for path in FIXTURES_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("prompt_id") not in {"query-intent", "extraction"}, (
            f"legacy flat live fixture must move under its provider directory: {path.name}"
        )


def test_capture_config_normalizes_provider_and_uses_contract_defaults(monkeypatch):
    for var in (GATE_VAR, MODEL_VAR, BASE_URL_VAR, API_KEY_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(GATE_VAR, "Claude")

    cfg = capture._live_config()

    assert cfg.provider == "claudecli"
    assert cfg.model == "sonnet"
    assert cfg.api_key is None
    assert cfg.base_url is None


def test_capture_config_uses_all_documented_companions(monkeypatch):
    monkeypatch.setenv(GATE_VAR, "openai")
    monkeypatch.setenv(MODEL_VAR, "synthetic-model")
    monkeypatch.setenv(BASE_URL_VAR, "http://synthetic.example/v1")
    monkeypatch.setenv(API_KEY_VAR, "synthetic-key")

    cfg = capture._live_config()

    assert (cfg.provider, cfg.model, cfg.base_url, cfg.api_key) == (
        "openai",
        "synthetic-model",
        "http://synthetic.example/v1",
        "synthetic-key",
    )


def test_capture_config_rejects_unknown_or_keyless_cloud_provider(monkeypatch):
    monkeypatch.setenv(GATE_VAR, "unknown")
    with pytest.raises(SystemExit, match="not a known provider"):
        capture._live_config()

    monkeypatch.setenv(GATE_VAR, "anthropic")
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    with pytest.raises(SystemExit, match="requires VN_CONTRACT_API_KEY"):
        capture._live_config()


def test_capture_patch_restores_selected_adapter_symbol_after_parser_failure():
    module = type(sys)("synthetic_adapter")

    def original(raw):
        raise RuntimeError(raw)

    module.parse_query_intent = original
    with capture._capture_raw(module, "parse_query_intent") as box:
        with pytest.raises(RuntimeError, match="synthetic raw"):
            module.parse_query_intent("synthetic raw")
    assert box == {"raw": "synthetic raw"}
    assert module.parse_query_intent is original


def test_capture_keeps_object_raw_responses_without_writing(tmp_path):
    module = type(sys)("synthetic_adapter")
    module.parse_query_intent = lambda raw: raw
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="synthetic-model",
        api_key="synthetic-key",
        base_url=None,
        llm_timeout_seconds=1,
    )

    class Client:
        def extract_query_intent(self, *, question):
            assert question == capture.QUERY_INTENT_QUESTION
            return module.parse_query_intent({"kind": "lookup_object"})

    payload = capture.capture_query_intent(cfg, Client(), module)
    assert payload["raw_response"] == {"kind": "lookup_object"}


def test_capture_main_writes_no_live_fixture_when_second_payload_fails(monkeypatch, tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claudecli",
        model="sonnet",
        api_key=None,
        base_url=None,
        llm_timeout_seconds=1,
    )
    writes = []
    monkeypatch.setattr(capture, "capture_sync_failure", lambda: None)
    monkeypatch.setattr(capture, "_live_config", lambda: cfg)
    monkeypatch.setattr(capture, "_adapter_module", lambda provider: object())
    monkeypatch.setattr(capture, "get_client", lambda config: object())
    monkeypatch.setattr(capture, "capture_query_intent", lambda *args: {"provider": "claudecli"})
    monkeypatch.setattr(
        capture,
        "capture_extraction",
        lambda *args: (_ for _ in ()).throw(SystemExit("failed")),
    )
    monkeypatch.setattr(capture, "_write", lambda *args: writes.append(args))

    with pytest.raises(SystemExit, match="failed"):
        capture.main()
    assert writes == []


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


def test_every_module_in_the_directory_is_accounted_for():
    """Every test module in this directory but this one is on a list above.

    `CONTRACT_MODULES` is hand-written and no other code reads this directory's
    listing, so dropping a module from it can look exactly like an accident: the
    parametrized check below stops running that case, and the module's guards
    can then be unmarked with nothing going red. What this assertion buys is one
    half of that. A module dropped from `CONTRACT_MODULES` and *not* recorded in
    `PROMOTED_GUARDS` fails here — that is the edit this change makes to
    `test_sync_rc_contract.py`, legal only because the same commit records the
    promotion.

    The other half it does not buy, and the summary line is the whole claim:
    `accounted` is a **union**, so a module with a guard named under it in
    `PROMOTED_GUARDS` satisfies it whether or not `CONTRACT_MODULES` still lists
    it. Dropping such a module from `CONTRACT_MODULES` and unmarking its
    remaining guards was measured silent across this directory.

    Closing that does not need the ledger derived from source. A listing rule —
    a module `CONTRACT_MODULES` does not name must have every `test_*` in it on
    the ledger — is green on this tree and does fail that edit; both measured.
    What stops it is the bill it sends the next promotion. Promoting
    `test_query_intent_contract.py`'s live guard strips that module's only
    marker, so the parametrized check below forces it out of `CONTRACT_MODULES`,
    and the rule then demands every remaining `test_*` there be entered under
    `PROMOTED_GUARDS` — including
    `test_deterministic_parser_does_not_resolve_the_role_question`, a control
    that was never gated and so was never promoted. Measured: the rule names
    that control alongside the guard actually being promoted. Entering it would
    be a false claim written into a data structure to satisfy an assertion.

    Against the listing on disk the equality does hold both ways: a module that
    appears without being listed fails, and a listed module that is not on disk
    fails.

    A module counts as promoted only when `PROMOTED_GUARDS` names at least one
    guard under it. Keying on membership alone would accept an empty tuple,
    which also parametrizes
    :func:`test_promoted_guards_carry_neither_the_marker_nor_the_gate` over no
    names: one line would then satisfy this assertion and empty that ledger at
    the same time, and it is the cheapest way to answer the message below.
    """
    on_disk = {path.name for path in CONTRACT_DIR.glob("test_*.py")}
    accounted = (
        set(CONTRACT_MODULES)
        | {name for name, guards in PROMOTED_GUARDS.items() if guards}
        | {Path(__file__).name}
    )
    assert on_disk == accounted, (
        "every test module in tests/contract must either declare a contract "
        "guard (CONTRACT_MODULES) or name a guard deliberately promoted into "
        "the default suite (PROMOTED_GUARDS, with the guard spelled out); this "
        "meta module is the one exception.\n"
        f"on disk but on neither list: {sorted(on_disk - accounted)}\n"
        f"listed but not on disk: {sorted(accounted - on_disk)}"
    )


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


def test_replay_targets_run_with_no_gate_at_all():
    """The replays must execute with the gate *unset* — the default suite's case.

    No ``gate_env`` on purpose. Since #270 these carry no ``contract`` marker and
    no ``require_opt_in``, so an unset gate has to run every one of them. Passing
    even the inert ``replay`` gate would satisfy a ``require_opt_in`` that crept
    back onto a single guard: the count below would still read full while that
    guard skipped in every default run.
    """
    result = _nested_pytest(*REPLAY_ONLY_TARGETS)
    assert result.returncode == 0, (
        "the replay guards should pass with no contract gate set at all.\n"
        f"{result.stdout}\n{result.stderr}"
    )
    expected_count = 2 * len(_valid_live_fixture_pairs()) + 1
    assert f"{expected_count} passed" in result.stdout, (
        "the replay guards did not run every discovered provider fixture pair "
        "and the Claude regression assertion.\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_deterministic_promoted_guards_run_with_no_gate_at_all():
    """A promoted guard whose name no replay target uses must run with no gate.

    :func:`test_promoted_guards_carry_neither_the_marker_nor_the_gate` reads the
    source, and a guard can skip for a reason the source still reads as correct.
    ``pytest.importorskip`` in the body is such a reason: the ``def`` says
    promoted, the run says skipped, and nothing is red. This executes those
    guards instead of reading them.

    No ``gate_env``, for the same reason as the replay targets above: any gate
    value would satisfy a ``require_opt_in`` that crept back onto one of these,
    and the count below would still read full while that guard skipped in every
    default run.

    The targets come from ``PROMOTED_GUARDS`` minus what
    ``REPLAY_ONLY_TARGETS`` already covers, and the expected count is the length
    of the list actually handed to the child, so there is no written number here
    to go stale. That subtraction can reach empty, and an empty list of node ids
    is not a smaller run — the assertion below refuses to spawn one, and comes
    before the ``importorskip`` because an empty ledger is a fact about the tree
    rather than about the environment.

    ``pytest.importorskip`` guards this test too. ``duckdb`` is a core
    dependency, and ``tests/test_analytics.py`` records the convention that a
    missing one is a broken or minimal environment to skip on rather than fail
    on; the guard checked here follows it. Without the same line, this pin would
    make ``duckdb`` a requirement for a green default suite — a change to the
    suite's environmental contract that no promotion asked for.
    """
    assert DETERMINISTIC_PROMOTED_TARGETS, (
        "PROMOTED_GUARDS no longer names a guard that REPLAY_ONLY_TARGETS does "
        "not also name, so this test has nothing to run. Handing pytest no node "
        "id at all is not a smaller run: the child would collect the whole suite "
        "from the repo root, this module included, and re-enter this test."
    )
    pytest.importorskip("duckdb")
    result = _nested_pytest(*DETERMINISTIC_PROMOTED_TARGETS)
    assert result.returncode == 0, (
        "the promoted deterministic guards should pass with no contract gate "
        f"set at all.\n{result.stdout}\n{result.stderr}"
    )
    assert f"{len(DETERMINISTIC_PROMOTED_TARGETS)} passed" in result.stdout, (
        "the run with no gate set did not report "
        f"{len(DETERMINISTIC_PROMOTED_TARGETS)} passed: either a promoted guard "
        "did not execute, or one of these node ids now collects more than one "
        f"test, as `@pytest.mark.parametrize` does.\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("module_name", sorted(PROMOTED_GUARDS))
def test_promoted_guards_carry_neither_the_marker_nor_the_gate(module_name):
    """A guard this ledger names still exists, without the marker or the opt-in gate.

    Two of the three reversions read for here cost default-suite coverage
    outright: ``require_opt_in`` coming back makes the guard skip there, and
    deleting the guard removes it. The marker is different — a marked test still
    runs under ``pytest tests`` — but it puts the guard back into the opt-in
    accounting, where it keeps ``conftest.py``'s all-skipped session guard
    permanently satisfied. All three are source-level edits, so reading the
    source catches them without spawning a pytest.

    Not every way is source-readable — let ``LIVE_FIXTURES``' glob stop matching
    and the parametrized replays collect nothing while their ``def`` still reads
    correctly here; that one is caught by the count in
    :func:`test_replay_targets_run_with_no_gate_at_all`. A ``pytest.importorskip``
    inside the body is another: the ``def`` goes on reading as promoted while the
    guard skips in every default run, which is what the count in
    :func:`test_deterministic_promoted_guards_run_with_no_gate_at_all` catches.

    Being source-readable is not enough either. The three assertions below are
    the whole of what this test reads, so an un-promotion it does not read still
    passes: taking the sibling gate ``require_live_provider`` instead of
    ``require_opt_in``, and ``@pytest.mark.skip``, both leave it green. Both
    were measured to be caught only by that same count — which begins with
    ``pytest.importorskip("duckdb")``, so where duckdb is missing neither is
    caught at all.

    ``PROMOTED_GUARDS`` is written out rather than discovered because of the
    deletion: a guard that no longer exists cannot be discovered from source
    that no longer mentions it. For a guard ``REPLAY_ONLY_TARGETS`` also names,
    a deletion that tidies away that entry and the matching term in
    :func:`test_replay_targets_run_with_no_gate_at_all`'s count leaves that test
    green, and this is the second, independent ledger that still goes red. For a
    guard named only here there is no first ledger — this is the record that the
    promotion happened, and the assertions below are what read the source for
    its reversal.

    The marker coming back does redden the harness elsewhere, through the meta
    tests built on an all-skipped run failing. What those report is a run that
    exited 0 while every guard skipped, or a wrapper that never reached pytest,
    which points a reader at the gate or at issue #273's wrapper. This test says
    instead that a promoted guard was re-marked.

    What it cannot see is its own record going away: a commit that removes a
    name from ``PROMOTED_GUARDS`` and re-gates that guard in the same edit
    leaves this test nothing to read. Whether anything else catches that turns
    on one thing. A module ``CONTRACT_MODULES`` does not list —
    ``test_sync_rc_contract.py`` today — has only its ledger entry holding it in
    :func:`test_every_module_in_the_directory_is_accounted_for`'s union, so
    removing that entry fails there. A module ``CONTRACT_MODULES`` still lists
    satisfies that union either way, and the same edit on the DuckDB control was
    measured to leave the directory green.
    """
    module = _load_module(CONTRACT_DIR / module_name)
    marked = set(_contract_test_names(module))
    for name in PROMOTED_GUARDS[module_name]:
        func = getattr(module, name, None)
        assert func is not None, (
            f"{module_name}::{name} is gone; it was promoted into the default "
            "suite, so removing it removes default-suite coverage — drop it from "
            "PROMOTED_GUARDS in the same commit if that is deliberate"
        )
        assert name not in marked, (
            f"{module_name}::{name} carries @pytest.mark.contract again. It "
            "still runs under `pytest tests`, but the marker puts it back into "
            "the opt-in accounting its promotion took it out of, where it keeps "
            "conftest.py's all-skipped session guard permanently satisfied"
        )
        assert "require_opt_in" not in inspect.signature(func).parameters, (
            f"{module_name}::{name} takes `require_opt_in` again; it was "
            "promoted into the default suite and that fixture skips it there"
        )


def test_targeting_the_contract_directory_fails_when_no_guard_runs():
    """`pytest tests/contract` with no gate must not be a green no-op either.

    The marker is not the only way to ask for these guards: naming the directory
    is the spelling a developer reaches for first. What the child below reports
    is the shape of the false green this arming prevents: gated guards skipping
    inside a run whose other tests pass, which is what makes an unarmed run look
    especially green. Both halves of that shape are re-derived from the child's
    own summary line below rather than quoted here, so no number in this
    docstring can go stale, and the test stops passing if the run stops being
    that shape — were every ungated test in this directory to disappear, the
    guards would still skip but nothing would pass alongside them.

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
    tallies = {
        outcome: int(count)
        for count, outcome in re.findall(r"(\d+) (passed|skipped)", result.stdout)
    }
    assert tallies.get("skipped") and tallies.get("passed"), (
        "this child run is no longer the false-green scenario the docstring "
        "describes — it needs both skipped guards and passing tests around "
        f"them, and reported {tallies}.\n{result.stdout}\n{result.stderr}"
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
        ("", "", [f"tests/contract/{MODULE_INSIDE_DIR}"], True, "a module inside it is named"),
        ("", "", [f"tests/contract/{MODULE_INSIDE_DIR}::test_x"], True, "a single test inside it is named"),
        ("", "", ["tests/contract/test_does_not_exist_at_all.py"], True, "a module inside it need not exist"),
        ("", "", ["tests.contract"], True, "--pyargs names it as a dotted module"),
        ("", "", [f"tests.contract.{MODULE_INSIDE_DIR.removesuffix('.py')}"], True, "--pyargs names a module inside"),
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
    """Default-suite meta tests must not spawn opted-in live contract runs.

    Any ``GATE_VAR`` in a ``_nested_pytest`` call is flagged, rather than checked
    against an allowed value. Until #270 the replay targets were run under an
    inert ``replay`` gate and had to be exempted; they now need no gate, so no
    call here sets one and an allow-list would only invite the inert gate back.
    """
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
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant):
                    resolved_key = key.value
                elif isinstance(key, ast.Name):
                    resolved_key = globals().get(key.id)
                else:
                    continue
                if resolved_key == GATE_VAR:
                    live_gate_calls.append(node.lineno)
    assert live_gate_calls == [], (
        f"_nested_pytest at line(s) {live_gate_calls} sets {GATE_VAR}; since "
        "#270 no meta test needs the contract gate, so any value here is "
        "flagged rather than checked against an allow-list"
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
