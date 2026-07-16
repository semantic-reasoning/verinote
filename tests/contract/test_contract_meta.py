# SPDX-License-Identifier: MPL-2.0
"""Meta guards for the #241 contract harness itself — deliberately *not* marked
``contract`` so they run in the default suite and stay green.

They catch the ways the harness could rot into a no-op: an unregistered marker
(so ``-m contract`` silently selects nothing), missing or provenance-less replay
fixtures, or a contract module that stops declaring any contract-marked test (so
the opt-in run collects zero guards). ``tests/contract/run.sh`` is the runtime
counterpart that fails a fully-skipped opt-in run.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parent
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
