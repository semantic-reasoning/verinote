# SPDX-License-Identifier: MPL-2.0
"""Review-gate transition guards (#231, #232, #257).

These tests pin the rule that a human's rejection is terminal: neither a toggle
nor an accept may revive a `superseded` fact, and the status-changing web routes
apply auto-accept so a decision on one fact reveals promotions of its siblings.
"""

from verinote.store import Store
from verinote.store.db import ENGINE_STATUSES


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _actions(store: Store, fact_id: int) -> list[str]:
    return [row["action"] for row in store.fact_log(fact_id)]


# --- #231: toggle_review must not promote superseded ----------------------


def test_toggle_leaves_superseded_untouched_and_unlogged(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("sources/a.txt")
    fact_id = s.add_fact(
        "Report", "published_year", "2024", status="superseded", source_id=source_id
    )
    log_before = _actions(s, fact_id)

    row = s.toggle_review(fact_id)

    # The rejected fact stays rejected — no revival to confirmed.
    assert row is not None
    assert row["status"] == "superseded"
    assert s.get_fact(fact_id)["status"] == "superseded"
    # No audit noise: the no-op toggle writes no `toggled` row.
    assert _actions(s, fact_id) == log_before
    assert "toggled" not in _actions(s, fact_id)
    # And it never becomes engine input.
    engine_ids = {r["id"] for r in s.facts(statuses=ENGINE_STATUSES)}
    assert fact_id not in engine_ids


def test_toggle_still_flips_review_and_engine_tiers(tmp_path):
    # Regression guard: the superseded fix must not freeze the normal toggle.
    s = _store(tmp_path)
    review_id = s.add_fact("Report", "author", "Kim", status="needs_review")
    engine_id = s.add_fact("Report", "author", "Lee", status="confirmed")

    assert s.toggle_review(review_id)["status"] == "confirmed"
    assert s.toggle_review(engine_id)["status"] == "needs_review"
