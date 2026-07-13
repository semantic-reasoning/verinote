# SPDX-License-Identifier: MPL-2.0
"""One runtime definition of the status tiers, proven by mutation.

`verinote.store.db` owns `ENGINE_STATUSES` / `REVIEW_STATUSES`. Every consumer
must answer "is this fact engine input?" from that one constant, *at call time*.
The failure this file exists to prevent is not hypothetical: consumers used to
do `from verinote.store import ENGINE_STATUSES`, which binds the frozenset
object at import time, so widening the tier moved `Store.engine_fact_terms()`
(a call-time lookup) while `build_query_schema_snapshot()` and
`ask.grounding_facts()` stayed on the old tier — the deterministic engine would
consume facts the planner's schema hint and the Ask/trust layers cannot see.

Each test below widens `db.ENGINE_STATUSES` (or empties a tier) and asserts the
consumer moves with it. Reverting a consumer to an import-time binding turns
these red.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from verinote.pipeline.acceptance import accept_recommendation, accept_recommendations
from verinote.pipeline.ask import grounding_facts
from verinote.pipeline.corroboration import (
    store_corroboration,
    store_single_valued_conflicts,
)
from verinote.pipeline.query_schema import build_query_schema_snapshot
from verinote.pipeline.trust import fact_trust_summary
from verinote.pipeline.workbench import trust_workbench
from verinote.store import Store, db


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _widen(monkeypatch) -> None:
    """Add `superseded` to the engine-input tier, the reviewer's mutation."""
    monkeypatch.setattr(db, "ENGINE_STATUSES", db.ENGINE_STATUSES | {"superseded"})


def test_engine_input_readers_agree_after_the_tier_is_widened(tmp_path, monkeypatch):
    """The reviewer's exact repro: Store saw the fact, schema and Ask did not.

    A `superseded` fact is invisible to every engine-input reader today. Widen
    the tier and all of them must see it together — otherwise the engine reasons
    over facts the planner's schema hint and Ask never learned about.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.md")
    store.add_fact("Acme", "uses", "FastAPI", status="superseded", source_id=source_id)

    # Before: nobody reads it.
    assert store.engine_fact_terms() == []
    assert build_query_schema_snapshot(store).fact_count == 0
    assert grounding_facts(store, question="Acme uses what?") == []

    _widen(monkeypatch)

    # After: everybody reads it, from the same constant, at call time.
    assert [row["id"] for row in store.engine_fact_terms()] == [1]
    snapshot = build_query_schema_snapshot(store)
    assert snapshot.fact_count == 1
    assert [r.relation.display for r in snapshot.relations] == ["uses"]
    grounded = grounding_facts(store, question="Acme uses what?")
    assert [(f.subject, f.relation, f.object) for f in grounded] == [
        ("Acme", "uses", "FastAPI")
    ]


def test_trust_follows_the_engine_tier(tmp_path, monkeypatch):
    """Trust labels a fact "engine input" from the tier, not from a stale copy."""
    store = _store(tmp_path)
    a = store.add_source("sources/a.md")
    b = store.add_source("sources/b.md")
    confirmed = store.add_fact("Acme", "uses", "FastAPI", status="confirmed", source_id=a)
    superseded = store.add_fact(
        "Acme", "uses", "FastAPI", status="superseded", source_id=b
    )

    before = fact_trust_summary(store, superseded)
    assert before.engine_input is False
    # The superseded twin does not corroborate the confirmed fact yet.
    assert fact_trust_summary(store, confirmed).support.source_count == 1

    _widen(monkeypatch)

    after = fact_trust_summary(store, superseded)
    assert after.engine_input is True
    # ...and now it is distinct source support, exactly like the engine sees it.
    assert fact_trust_summary(store, confirmed).support.source_count == 2


def test_corroboration_and_conflicts_follow_the_engine_tier(tmp_path, monkeypatch):
    """The dashboard's corroboration/conflict sections must not lag the card."""
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("established_on").\n',
        encoding="utf-8",
    )
    store = _store(tmp_path)
    a = store.add_source("sources/a.md")
    b = store.add_source("sources/b.md")
    store.add_fact("Acme", "uses", "FastAPI", status="confirmed", source_id=a)
    store.add_fact("Acme", "uses", "FastAPI", status="superseded", source_id=b)
    store.add_fact("Org", "established_on", "2020", status="confirmed", source_id=a)
    store.add_fact("Org", "established_on", "2021", status="superseded", source_id=b)

    def _support() -> dict[tuple[str, str, str], tuple[str, ...]]:
        return {
            (f.subject, f.relation, f.object): f.sources
            for f in store_corroboration(store)
        }

    assert _support()[("Acme", "uses", "FastAPI")] == ("sources/a.md",)
    assert list(store_single_valued_conflicts(store)) == []
    bench = trust_workbench(store)
    assert [g.sources for g in bench.corroborated] == []
    assert list(bench.conflicts) == []

    _widen(monkeypatch)

    assert _support()[("Acme", "uses", "FastAPI")] == ("sources/a.md", "sources/b.md")
    conflicts = store_single_valued_conflicts(store)
    assert [(c.subject, c.relation) for c in conflicts] == [("Org", "established_on")]
    bench = trust_workbench(store)
    assert [g.sources for g in bench.corroborated] == [("sources/a.md", "sources/b.md")]
    assert [(c.subject, c.relation) for c in bench.conflicts] == [
        ("Org", "established_on")
    ]


def test_acceptance_support_follows_the_engine_tier(tmp_path, monkeypatch):
    """Auto-accept counts distinct source support over the *current* engine tier."""
    store = _store(tmp_path)
    a = store.add_source("sources/a.md")
    b = store.add_source("sources/b.md")
    target = store.add_fact(
        "Acme", "uses", "FastAPI", status="needs_review", source_id=a
    )
    store.add_fact("Acme", "uses", "FastAPI", status="superseded", source_id=b)

    before = accept_recommendation(store, target)
    assert before.support_sources == ("sources/a.md",)
    assert "insufficient_distinct_source_support" in before.reasons

    _widen(monkeypatch)

    after = accept_recommendation(store, target)
    assert after.support_sources == ("sources/a.md", "sources/b.md")
    assert "insufficient_distinct_source_support" not in after.reasons


def test_engine_input_consumers_refuse_an_empty_engine_tier(tmp_path, monkeypatch):
    """An empty tier is a crash, not a silent "no facts", in every consumer."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.md")
    fact_id = store.add_fact(
        "Acme", "uses", "FastAPI", status="confirmed", source_id=source_id
    )

    monkeypatch.setattr(db, "ENGINE_STATUSES", frozenset())

    for call in (
        lambda: build_query_schema_snapshot(store),
        lambda: grounding_facts(store, question="Acme uses what?"),
        lambda: fact_trust_summary(store, fact_id),
        lambda: store_corroboration(store),
        lambda: store_single_valued_conflicts(store),
        lambda: trust_workbench(store),
        lambda: accept_recommendation(store, fact_id),
    ):
        with pytest.raises(ValueError, match="must not be empty"):
            call()


def test_acceptance_refuses_an_empty_review_tier(tmp_path, monkeypatch):
    """The human gate must not quietly recommend nothing when the tier is empty."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.md")
    fact_id = store.add_fact(
        "Acme", "uses", "FastAPI", status="needs_review", source_id=source_id
    )

    monkeypatch.setattr(db, "REVIEW_STATUSES", frozenset())

    with pytest.raises(ValueError, match="must not be empty"):
        accept_recommendations(store)
    with pytest.raises(ValueError, match="must not be empty"):
        accept_recommendation(store, fact_id)
    with pytest.raises(ValueError, match="must not be empty"):
        trust_workbench(store)

    # The fact was not promoted behind our back.
    assert store.get_fact(fact_id)["status"] == "needs_review"


def test_no_module_binds_a_status_tier_at_import_time():
    """The regression is an import statement, so guard the import statement.

    `from verinote.store import ENGINE_STATUSES` (or from `verinote.store.db`)
    freezes the tier at import time and re-opens the split above. `db.py` owns
    the constants and `tiers.py` is the single call-time accessor; nobody else
    may name them in an import.
    """
    package = Path(__file__).resolve().parent.parent / "verinote"
    allowed = {package / "store" / "db.py", package / "store" / "tiers.py"}
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                bound = names & {"ENGINE_STATUSES", "REVIEW_STATUSES"}
                if bound:
                    offenders.append(f"{path.name}: {sorted(bound)}")
    assert offenders == []
