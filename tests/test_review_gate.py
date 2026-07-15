# SPDX-License-Identifier: MPL-2.0
"""Review-gate transition guards (#231, #232, #257).

These tests pin the rule that a human's rejection is terminal: neither a toggle
nor an accept may revive a `superseded` fact, and the status-changing web routes
apply auto-accept so a decision on one fact reveals promotions of its siblings.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.store import Store  # noqa: E402
from verinote.store.db import ENGINE_STATUSES  # noqa: E402
from verinote.web import create_app  # noqa: E402


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _auto_accept_client(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
        auto_accept_recommendations=True,
    )
    client = TestClient(create_app(cfg))
    return client, client.app.state.store


def _done_job(store: Store, source_id: int) -> int:
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id, source_id=source_id, chunks=["body"]
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_done(chunk_id, candidates=1)
    store.finish_extraction_job(job_id)
    return job_id


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


# --- #232: accept/reject are transition-aware, not blind writes -----------


def test_reject_then_accept_keeps_a_fact_superseded(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="needs_review")

    assert s.reject_fact(fact_id)["status"] == "superseded"
    # The whole point of the gate: accept must not resurrect a rejection.
    reaccepted = s.accept_fact(fact_id)
    assert reaccepted["status"] == "superseded"
    assert s.get_fact(fact_id)["status"] == "superseded"
    # One rejection logged, and no `accepted` row from the refused accept.
    actions = _actions(s, fact_id)
    assert actions.count("rejected") == 1
    assert "accepted" not in actions


def test_reaccepting_a_confirmed_fact_is_a_silent_noop(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="confirmed")
    log_before = _actions(s, fact_id)

    row = s.accept_fact(fact_id)

    assert row["status"] == "confirmed"
    # No write, no audit growth — accept only moves review-tier rows.
    assert _actions(s, fact_id) == log_before


def test_accept_promotes_a_review_fact(tmp_path):
    # Regression guard: the transition checks must not block the real accept.
    s = _store(tmp_path)
    candidate = s.add_fact("Report", "author", "Kim", status="candidate")
    needs = s.add_fact("Report", "author", "Lee", status="needs_review")

    assert s.accept_fact(candidate)["status"] == "confirmed"
    assert s.accept_fact(needs)["status"] == "confirmed"
    assert _actions(s, candidate) == ["accepted"]


def test_double_reject_logs_once(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="needs_review")

    s.reject_fact(fact_id)
    s.reject_fact(fact_id)

    # A repeat reject on an already-superseded fact writes no duplicate audit row.
    assert _actions(s, fact_id).count("rejected") == 1


# --- #257: decision routes apply auto-accept and refresh siblings ---------


def test_accept_route_promotes_a_corroborated_sibling_and_refreshes(tmp_path):
    client, store = _auto_accept_client(tmp_path)
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = _done_job(store, source_a)
    job_b = _done_job(store, source_b)
    acted = store.add_fact(
        "Report", "author", "Kim",
        status="needs_review", source_id=source_a, job_id=job_a,
    )
    sibling = store.add_fact(
        "Report", "author", "Kim",
        status="needs_review", source_id=source_b, job_id=job_b,
    )

    resp = client.post(f"/facts/{acted}/accept")

    assert resp.status_code == 200
    # The acted fact is confirmed by the route; auto-accept then promotes the
    # corroborating sibling, which a single-row swap can't show — hence a refresh.
    assert store.get_fact(acted)["status"] == "confirmed"
    assert store.get_fact(sibling)["status"] == "accepted"
    assert resp.headers.get("HX-Refresh") == "true"


def test_accept_route_without_promotions_keeps_single_row_swap(tmp_path):
    client, store = _auto_accept_client(tmp_path)
    source = store.add_source("sources/solo.txt")
    job = _done_job(store, source)
    acted = store.add_fact(
        "Solo", "author", "Han",
        status="needs_review", source_id=source, job_id=job,
    )

    resp = client.post(f"/facts/{acted}/accept")

    assert resp.status_code == 200
    assert store.get_fact(acted)["status"] == "confirmed"
    # Nothing else was eligible, so no full refresh — the row swap stands.
    assert "HX-Refresh" not in resp.headers
