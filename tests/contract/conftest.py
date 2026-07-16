# SPDX-License-Identifier: MPL-2.0
"""Opt-in gate and provider wiring for the issue #241 provider contract tests.

These tests exist to catch failures that *only* show up against a real LLM
provider (issues #237/#238) or that the deterministic suite would otherwise
paper over (issue #239).

The gate lives in a ``VN_CONTRACT_*`` namespace, deliberately *outside* the
``VERINOTE_*`` prefix. The root ``tests/conftest.py`` sandbox drops every
``VERINOTE_*`` variable at session start so an ambient export cannot change what
a test sees; a gate under that prefix would be erased before any fixture could
read it. Naming it out of the sandbox's way means the gate and its companion
settings are simply readable at fixture time, from any invocation path, with no
snapshot and no ordering race (issue #272).

Two gates share it:

* :func:`require_live_provider` gates the tests that actually call the provider.
  When the gate is unset it *skips* (opt-in). When the gate is set but the
  provider is unreachable (e.g. the ``claude`` binary is missing) it *fails*,
  never skips — a provider you asked to exercise but that cannot run is a real
  failure, not an absence of coverage (issue #234).
* :func:`require_opt_in` gates the deterministic contract tests (replay and the
  sync exit-code guard). They need no live provider but must still stay out of
  the default suite, so they skip on the same unset gate.

:func:`pytest_sessionfinish` closes the harness's worst failure mode: asking for
contract tests and getting a green run that exercised nothing. Whenever a run
asks for these guards — by naming the ``contract`` marker *or* a path in this
directory (see :func:`arms_skip_guard`) — and not one selected test executes,
the session fails. The default suite asks for neither, so it is untouched and
the guards keep self-skipping there.

Contract tests build their own :class:`~verinote.config.Config` directly rather
than through the environment, mirroring ``tests/test_ollama_adapter.py``'s
``_cfg`` helper, so the sandboxed environment does not starve them of a provider.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from verinote.config import Config
from verinote.llm import get_client
from verinote.llm.base import LLMClient

GATE_VAR = "VN_CONTRACT_PROVIDER"
MODEL_VAR = "VN_CONTRACT_MODEL"
BASE_URL_VAR = "VN_CONTRACT_BASE_URL"
API_KEY_VAR = "VN_CONTRACT_API_KEY"
GATE_HINT = f"set {GATE_VAR}=claudecli|ollama|... to run (issue #241)"


def contract_provider() -> str | None:
    """The opt-in provider id the run was launched with, or ``None`` if unset."""
    return os.environ.get(GATE_VAR) or None


def _config_for(provider: str, root) -> Config:
    """Build a Config for `provider` from the ``VN_CONTRACT_*`` settings."""
    root.mkdir(parents=True, exist_ok=True)
    if provider == "claudecli":
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider="claudecli",
            model=os.environ.get(MODEL_VAR) or "sonnet",
            api_key=None,
            base_url=None,
            llm_timeout_seconds=180.0,
        )
    if provider == "ollama":
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider="ollama",
            model=os.environ.get(MODEL_VAR) or "llama3.1",
            api_key=None,
            base_url=os.environ.get(BASE_URL_VAR) or "http://localhost:11434",
            llm_timeout_seconds=180.0,
        )
    if provider in ("anthropic", "openai"):
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=os.environ.get(MODEL_VAR)
            or ("claude-opus-4-8" if provider == "anthropic" else "gpt-4o"),
            api_key=os.environ.get(API_KEY_VAR) or None,
            base_url=None,
            llm_timeout_seconds=180.0,
        )
    pytest.fail(f"{GATE_VAR}={provider!r} is not a known provider (expected claudecli|ollama|anthropic|openai)")


def _assert_provider_available(provider: str, cfg: Config) -> None:
    """Fail (never skip) when the requested provider cannot actually be reached.

    Skipping here would let a broken provider masquerade as "no coverage" — the
    exact hole issue #234 closes. The gate was set on purpose, so an unreachable
    provider is a hard failure.
    """
    if provider == "claudecli":
        if shutil.which("claude") is None:
            pytest.fail(
                f"{GATE_VAR}=claudecli but the `claude` binary is not on PATH; "
                "install Claude Code (a set gate must fail, not skip — issue #234)"
            )
        return
    if provider in ("anthropic", "openai"):
        if not cfg.api_key:
            pytest.fail(
                f"{GATE_VAR}={provider} but no API key was provided; "
                f"set {API_KEY_VAR} (a set gate must fail, not skip — issue #234)"
            )
        return
    # ollama and any other reachable-over-network provider: leave reachability to
    # the first live call, which raises LLMError the guards already assert on.


# --- "selected but never ran" session guard -------------------------------
#
# Tracked across the session and consulted in `pytest_sessionfinish`.

CONTRACT_DIR = Path(__file__).resolve().parent

_selected_contract_nodeids: set[str] = set()
_executed_contract_ids: set[str] = set()


def _points_inside_contract_dir(arg: str, invocation_dir: Path) -> bool:
    """Is this pytest positional argument a path in (or at) `tests/contract`?"""
    path = Path(str(arg).split("::")[0])
    if not path.is_absolute():
        path = invocation_dir / path
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == CONTRACT_DIR or CONTRACT_DIR in resolved.parents


def arms_skip_guard(markexpr: str, args: list[str], invocation_dir: Path) -> bool:
    """Did this invocation ask for the contract tests specifically?

    Two spellings mean "I want the contract guards": naming the marker, and
    naming a path in this directory. Both must arm the skipped-run guard —
    `pytest tests/contract` is the spelling a developer reaches for first, and
    left unarmed it reports "18 passed, 7 skipped" and exits 0 while not one
    guard ran, which is the false green this harness exists to prevent.

    A run that merely *collects* this directory on its way through the tree must
    not arm: `pytest` (which `testpaths` expands to `tests`) and `pytest tests`
    both pass `tests`, a parent of this directory, not a path inside it. So the
    default suite keeps its self-skipping guards and stays green.

    Kept a pure function of the three inputs so the arming boundary can be pinned
    directly, without a pytest session per case.
    """
    if "contract" in (markexpr or ""):
        return True
    return any(_points_inside_contract_dir(arg, invocation_dir) for arg in args)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Count the contract tests this run actually intends to execute.

    `trylast` matters: it puts this after the mark plugin's deselection, so
    `items` holds what survived `-m`. Running earlier would count guards that
    `-m "not contract"` is about to deselect and then fail the session because
    they never ran — a false red, the mirror of the bug this guard fixes.
    """
    markexpr = getattr(config.option, "markexpr", "") or ""
    if not arms_skip_guard(markexpr, list(config.args), Path(config.invocation_params.dir)):
        return
    _selected_contract_nodeids.update(
        item.nodeid for item in items if item.get_closest_marker("contract")
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Remember every contract test that got past the gate.

    Membership is by collected node id, not `report.keywords`: this directory is
    a package *named* `contract`, so every test under it — the meta guards and
    the ungated controls included — carries `contract` as a keyword. Keying on
    that would let any passing neighbour stand in for a guard that never ran.

    Only the call phase counts as "ran": a skipped test still reports a *passing*
    teardown, so accepting any non-skipped phase would call every skip an
    execution and defeat the guard below. A setup/teardown *failure* counts too —
    a fixture that fails the gate on an unreachable provider (issue #234) is the
    harness working, not a silent no-op.
    """
    if report.nodeid not in _selected_contract_nodeids:
        return
    if report.failed or (report.when == "call" and report.outcome != "skipped"):
        _executed_contract_ids.add(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a run that asked for contract tests and executed none of them.

    Without this, `pytest -m contract` with the gate unset reports "N skipped"
    and exits 0 — the harness's worst outcome, a green run that guarded nothing.
    An already-failing session keeps its own status.

    `--collect-only` is exempt: not running tests is what it was asked to do, so
    failing it would be the mirror image of the bug above — a red run that had
    nothing to report.
    """
    if session.config.option.collectonly:
        return
    if not _selected_contract_nodeids or _executed_contract_ids:
        return
    if exitstatus != 0:
        return
    message = (
        f"{len(_selected_contract_nodeids)} contract test(s) were selected but every one "
        f"skipped: no guard executed. {GATE_HINT}"
    )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "contract gate", red=True, bold=True)
        reporter.write_line(message)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def require_opt_in() -> str:
    """Skip unless the opt-in gate is set. For deterministic contract tests."""
    provider = contract_provider()
    if not provider:
        pytest.skip(GATE_HINT)
    return provider


@pytest.fixture
def contract_client(tmp_path) -> LLMClient:
    """A live `LLMClient` for the opt-in provider, or skip when the gate is unset."""
    provider = contract_provider()
    if not provider:
        pytest.skip(GATE_HINT)
    cfg = _config_for(provider, tmp_path / "kb")
    _assert_provider_available(provider, cfg)
    return get_client(cfg)


@pytest.fixture
def require_live_provider(contract_client) -> LLMClient:
    """Alias that reads as a precondition at the call site; yields the live client."""
    return contract_client
