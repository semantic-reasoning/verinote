# SPDX-License-Identifier: MPL-2.0
"""Contract guard for issue #238: a founding-date fact the live extractor produces
must normalise into the policy's *functional* relation vocabulary, so the
functional-conflict check can fire when a source states two different dates.

The functional vocabulary is not hard-coded here: it is parsed from
``engine.wirelog.DEFAULT_POLICY``'s ``functional("...")`` declarations, the same
program the verifier runs. A relation the extractor emits (e.g. ``founded`` /
``established``) that does not normalise into that set leaves the conflict check
blind to the contradiction — the #238 failure. On ``origin/main`` (no #238 fix)
the live and replay assertions are expected to fail; the differential test is a
positive control that stays green to prove the check itself works.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from verinote.engine import DEFAULT_POLICY
from verinote.llm.schema import parse_facts
from verinote.pipeline.corroboration import (
    canonical_relation,
    functional_relations,
    relation_aliases,
)
from verinote.policy_defaults import DEFAULT_RELATION_ALIASES

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "contract" / "extraction_acme_two_dates.json"

_YEAR = re.compile(r"20(?:20|21)")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _founding_relations(facts) -> set[str]:
    """Relations linking Acme Robotics to one of the two founding years, normalised.

    Identifies founding facts structurally (subject is the entity, object carries
    the year) rather than by hard-coded relation label — the label is exactly what
    #238 is about, so keying on it would beg the question.
    """
    aliases = relation_aliases(DEFAULT_RELATION_ALIASES)
    founding = [
        f
        for f in facts
        if "acme" in f.subject.lower() and _YEAR.search(f.object)
    ]
    assert founding, "extractor produced no Acme founding-date fact"
    return {canonical_relation(f.relation, aliases) for f in founding}


@pytest.mark.contract
def test_live_founding_relation_normalizes_into_functional_vocab(require_live_provider):
    client = require_live_provider
    facts = client.extract_facts(
        source_text=_fixture()["input"],
    )
    functional = functional_relations(DEFAULT_POLICY)
    normalized = _founding_relations(facts)
    assert normalized & functional, (
        f"founding relations {sorted(normalized)} do not normalise into the policy's "
        f"functional vocabulary {sorted(functional)}; the functional-conflict check "
        "cannot see the two-date contradiction (#238)"
    )


@pytest.mark.contract
def test_replay_founding_relation_normalizes_into_functional_vocab(require_opt_in):
    fixture = _fixture()
    facts = parse_facts(fixture["raw_response"])
    # Non-vacuity: the capture must actually contain both founding years, or the
    # replay would pass without ever testing the contradiction it exists for.
    years = {m.group(0) for f in facts for m in [_YEAR.search(f.object)] if m}
    assert {"2020", "2021"} <= years, f"fixture is missing a founding year: {sorted(years)}"
    functional = functional_relations(DEFAULT_POLICY)
    normalized = _founding_relations(facts)
    assert normalized & functional, (
        f"founding relations {sorted(normalized)} do not normalise into the policy's "
        f"functional vocabulary {sorted(functional)} (#238)"
    )


@pytest.mark.contract
def test_functional_conflict_fires_on_two_dates(require_opt_in):
    """Positive control: once a founding relation is functional, two dates conflict.

    Differential — feeds normalised facts to the real DuckDB verifier. A
    functional relation with two objects must error; a non-functional one must
    not. This stays green on every branch; it proves the conflict machinery the
    live/replay guards depend on is real, not a stub.
    """
    pytest.importorskip("duckdb")
    from verinote.engine.duckdb_backend import run_check_duckdb

    functional = functional_relations(DEFAULT_POLICY)
    assert functional, "policy declares no functional relations"
    rel = "established_on" if "established_on" in functional else sorted(functional)[0]

    conflict = run_check_duckdb(
        [
            {"subject": "Acme Robotics", "relation": rel, "object": "2020"},
            {"subject": "Acme Robotics", "relation": rel, "object": "2021"},
        ]
    )
    assert conflict.engine_available is True
    assert conflict.errors == 1
    assert any("functional_conflict" in finding for finding in conflict.findings)

    non_functional = "founded"
    assert non_functional not in functional
    clean = run_check_duckdb(
        [
            {"subject": "Acme Robotics", "relation": non_functional, "object": "2020"},
            {"subject": "Acme Robotics", "relation": non_functional, "object": "2021"},
        ]
    )
    assert clean.errors == 0
