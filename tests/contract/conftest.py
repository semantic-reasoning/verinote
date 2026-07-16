# SPDX-License-Identifier: MPL-2.0
"""Opt-in gate and provider wiring for the issue #241 provider contract tests.

These tests exist to catch failures that *only* show up against a real LLM
provider (issues #237/#238) or that the deterministic suite would otherwise
paper over (issue #239). The root `tests/conftest.py` autouse sandbox strips
every ``VERINOTE_*`` variable before any test runs, so the opt-in signal cannot
be read from the environment at test time. It is snapshotted at *this module's
import time* (see ``_contract_provider`` below) — the earliest point available,
before the sandbox's ``pytest_configure`` seals the environment on the direct
opt-in path — with ``pytest_configure`` here as a fallback. Both are exposed
through :func:`contract_provider`.

Two gates share that snapshot:

* :func:`require_live_provider` gates the tests that actually call the provider.
  When the gate is unset it *skips* (opt-in). When the gate is set but the
  provider is unreachable (e.g. the ``claude`` binary is missing) it *fails*,
  never skips — a provider you asked to exercise but that cannot run is a real
  failure, not an absence of coverage (issue #234).
* :func:`require_opt_in` gates the deterministic contract tests (replay and the
  sync exit-code guard). They need no live provider but must still stay out of
  the default suite, so they skip on the same unset gate.

Contract tests build their own :class:`~verinote.config.Config` directly rather
than through the environment, mirroring ``tests/test_ollama_adapter.py``'s
``_cfg`` helper, so the stripped environment does not starve them of a provider.
"""

from __future__ import annotations

import os
import shutil

import pytest

from verinote.config import Config
from verinote.llm import get_client
from verinote.llm.base import LLMClient

GATE_VAR = "VERINOTE_CONTRACT_PROVIDER"
GATE_HINT = f"set {GATE_VAR}=claudecli|ollama|... to run (issue #241)"

# Snapshot of the opt-in gate, taken as early as possible so the root sandbox's
# ``VERINOTE_*`` strip cannot erase it before a test reads it.
#
# The strip lives in the root ``tests/conftest.py``'s ``pytest_configure`` and
# deletes the variable from ``os.environ`` for the whole session. Capturing it
# here at *conftest import time* wins the race: when this file is on the direct
# invocation path (``pytest tests/contract ...`` — the documented opt-in run, and
# what this repo's #241 validation uses), pytest imports every conftest at
# startup *before* any ``pytest_configure`` fires, so the gate is still present.
# ``pytest_configure`` below is a fallback for the same reason (it runs before
# collection when this conftest is loaded early). Neither can see the gate if the
# suite is discovered from a parent directory that loads this conftest only
# mid-collection, after the strip — hence opt-in runs must target ``tests/contract``.
_contract_provider: str | None = os.environ.get(GATE_VAR)


def pytest_configure(config: pytest.Config) -> None:
    """Fallback capture of the opt-in gate, in case import ran before this file.

    Import-time capture above is the primary path; this only fills the snapshot
    if it is still unset and the ambient variable survives to configure time.
    """
    global _contract_provider
    if _contract_provider is None:
        _contract_provider = os.environ.get(GATE_VAR)


def contract_provider() -> str | None:
    """The opt-in provider id the run was launched with, or ``None`` if unset."""
    return _contract_provider


def _config_for(provider: str, root) -> Config:
    """Build a Config for `provider` without reading the (stripped) environment."""
    root.mkdir(parents=True, exist_ok=True)
    if provider == "claudecli":
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider="claudecli",
            model="sonnet",
            api_key=None,
            base_url=None,
            llm_timeout_seconds=180.0,
        )
    if provider == "ollama":
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider="ollama",
            model=os.environ.get("VERINOTE_CONTRACT_MODEL", "llama3.1"),
            api_key=None,
            base_url=os.environ.get("VERINOTE_CONTRACT_BASE_URL", "http://localhost:11434"),
            llm_timeout_seconds=180.0,
        )
    if provider in ("anthropic", "openai"):
        return Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=os.environ.get("VERINOTE_CONTRACT_MODEL", "")
            or ("claude-opus-4-8" if provider == "anthropic" else "gpt-4o"),
            api_key=os.environ.get("VERINOTE_CONTRACT_API_KEY"),
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
                "set VERINOTE_CONTRACT_API_KEY (a set gate must fail, not skip — issue #234)"
            )
        return
    # ollama and any other reachable-over-network provider: leave reachability to
    # the first live call, which raises LLMError the guards already assert on.


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
