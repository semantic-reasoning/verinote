# SPDX-License-Identifier: MPL-2.0
"""Review-gate transition guards (#231, #232, #257).

These tests pin the rule that a human's rejection is terminal: neither a toggle
nor an accept may revive a `superseded` fact, and the status-changing web routes
apply auto-accept so a decision on one fact reveals promotions of its siblings.
"""

import inspect
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.pipeline import acceptance  # noqa: E402
from verinote.store import Store  # noqa: E402
from verinote.store.db import ENGINE_STATUSES  # noqa: E402
from verinote.web import create_app  # noqa: E402


def _store_at(db_path) -> Store:
    s = Store(db_path)
    s.init_schema()
    return s


def _store(tmp_path) -> Store:
    return _store_at(tmp_path / "kb.sqlite")


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

    decision = s.toggle_review(fact_id)

    # The rejected fact stays rejected — no revival to confirmed.
    assert decision.fact is not None
    assert decision.fact["status"] == "superseded"
    # And the store says so: no transition happened, so a caller owes it no
    # follow-on effects.
    assert decision.changed is False
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

    for fact_id, expected in ((review_id, "confirmed"), (engine_id, "needs_review")):
        decision = s.toggle_review(fact_id)
        assert decision.fact["status"] == expected
        # A real flip is reported as one.
        assert decision.changed is True


# --- #232: accept/reject are transition-aware, not blind writes -----------


def test_reject_then_accept_keeps_a_fact_superseded(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="needs_review")

    rejected = s.reject_fact(fact_id)
    assert rejected.fact["status"] == "superseded"
    assert rejected.changed is True
    # The whole point of the gate: accept must not resurrect a rejection.
    reaccepted = s.accept_fact(fact_id)
    assert reaccepted.fact["status"] == "superseded"
    assert reaccepted.changed is False
    assert s.get_fact(fact_id)["status"] == "superseded"
    # One rejection logged, and no `accepted` row from the refused accept.
    actions = _actions(s, fact_id)
    assert actions.count("rejected") == 1
    assert "accepted" not in actions


def test_reaccepting_a_confirmed_fact_is_a_silent_noop(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="confirmed")
    log_before = _actions(s, fact_id)

    decision = s.accept_fact(fact_id)

    assert decision.fact["status"] == "confirmed"
    # No write, no audit growth — accept only moves review-tier rows. The
    # `changed=False` is what lets a caller tell this apart from a real accept
    # that happens to land on the same status.
    assert decision.changed is False
    assert _actions(s, fact_id) == log_before


def test_accept_promotes_a_review_fact(tmp_path):
    # Regression guard: the transition checks must not block the real accept.
    s = _store(tmp_path)
    candidate = s.add_fact("Report", "author", "Kim", status="candidate")
    needs = s.add_fact("Report", "author", "Lee", status="needs_review")

    for fact_id in (candidate, needs):
        decision = s.accept_fact(fact_id)
        assert decision.fact["status"] == "confirmed"
        assert decision.changed is True
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


# --- #263 review: a decision must not be undone by the pass that follows it ---


def _corroborated_pair(store: Store, *, acted_status: str, sibling_status: str):
    """Two facts stating the same triple from two analysed sources.

    That is exactly the shape auto-accept calls eligible, so either fact is a
    promotion candidate the moment it sits in the review tier.
    """
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = _done_job(store, source_a)
    job_b = _done_job(store, source_b)
    acted = store.add_fact(
        "Report", "author", "Kim",
        status=acted_status, source_id=source_a, job_id=job_a,
    )
    sibling = store.add_fact(
        "Report", "author", "Kim",
        status=sibling_status, source_id=source_b, job_id=job_b,
    )
    return acted, sibling


def test_toggle_demotion_is_not_undone_by_auto_accept(tmp_path):
    # A human demoting a confirmed fact is an explicit decision. Auto-accept
    # runs in the same request for the siblings it may unblock, and it must not
    # use that pass to shove the demoted fact straight back up to `accepted`.
    client, store = _auto_accept_client(tmp_path)
    acted, _sibling = _corroborated_pair(
        store, acted_status="confirmed", sibling_status="confirmed"
    )

    resp = client.post(f"/facts/{acted}/toggle")

    assert resp.status_code == 200
    assert store.get_fact(acted)["status"] == "needs_review"
    # The demotion stands in the audit trail too: no rule undid it.
    assert "auto_accepted" not in _actions(store, acted)


def test_toggle_demotion_still_lets_auto_accept_promote_siblings(tmp_path):
    # Regression guard: excusing the acted fact must not switch auto-accept off
    # for everyone else. Demoting the engine-tier fact frees the single-valued
    # slot its sibling was conflicting on, so the sibling becomes eligible.
    client, store = _auto_accept_client(tmp_path)
    acted, sibling = _corroborated_pair(
        store, acted_status="confirmed", sibling_status="needs_review"
    )

    resp = client.post(f"/facts/{acted}/toggle")

    assert resp.status_code == 200
    assert store.get_fact(acted)["status"] == "needs_review"
    assert store.get_fact(sibling)["status"] == "accepted"
    # A sibling moved, and a single-row swap cannot show it.
    assert resp.headers.get("HX-Refresh") == "true"


def test_toggle_promotion_still_works_under_auto_accept(tmp_path):
    # The other direction of the toggle keeps promoting, as before.
    client, store = _auto_accept_client(tmp_path)
    store_fact = store.add_fact("Solo", "author", "Han", status="needs_review")

    resp = client.post(f"/facts/{store_fact}/toggle")

    assert resp.status_code == 200
    assert store.get_fact(store_fact)["status"] == "confirmed"


def test_toggle_cannot_overwrite_a_reject_that_lands_mid_transition(tmp_path):
    """A toggle that read `needs_review` must not write `confirmed` over a
    rejection that landed after that read.

    The two `Store` objects are the point: `Store._lock` only serialises writers
    sharing one instance, and a real deployment has several connections on one
    SQLite file. So the guard has to live in the write itself, conditional on
    the status the toggle actually observed.
    """
    db = tmp_path / "kb.sqlite"
    toggling = _store_at(db)
    rejecting = _store_at(db)
    fact_id = toggling.add_fact("Report", "author", "Kim", status="needs_review")

    real_get_fact = toggling.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved:
            interleaved.append(True)
            # The other connection's human presses reject in this window.
            rejecting.reject_fact(fact_id)
        return row

    toggling.get_fact = reject_once_after_the_read
    decision = toggling.toggle_review(fact_id)

    assert interleaved, "the reject never landed — the test proves nothing"
    # The rejection is terminal: the stale toggle target loses.
    assert rejecting.get_fact(fact_id)["status"] == "superseded"
    assert decision.fact["status"] == "superseded"
    # The loser reports its loss, so the route above it skips the follow-on pass.
    assert decision.changed is False
    # And the losing toggle logged no `toggled` row for a write it never made.
    assert "toggled" not in _actions(rejecting, fact_id)


def test_accept_cannot_overwrite_a_reject_that_lands_mid_transition(tmp_path):
    """The same window `toggle_review` had, in `accept_fact`.

    Checking the tier before the write only settles the race for callers sharing
    this instance's lock. A reject arriving on another connection to the same KB
    lands between that check and the write, and a blind write would then take a
    fact back out of `superseded` — the one thing the review gate promises can't
    happen.
    """
    db = tmp_path / "kb.sqlite"
    accepting = _store_at(db)
    rejecting = _store_at(db)
    fact_id = accepting.add_fact("Report", "author", "Kim", status="needs_review")

    real_get_fact = accepting.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved:
            interleaved.append(True)
            # The other connection's human presses reject in this window.
            rejecting.reject_fact(fact_id)
        return row

    accepting.get_fact = reject_once_after_the_read
    decision = accepting.accept_fact(fact_id)

    assert interleaved, "the reject never landed — the test proves nothing"
    # The rejection is terminal: the stale accept loses.
    assert rejecting.get_fact(fact_id)["status"] == "superseded"
    assert decision.fact["status"] == "superseded"
    # The loser reports its loss, so the route above it skips the follow-on pass.
    assert decision.changed is False
    # And the losing accept logged no `accepted` row for a write it never made.
    assert "accepted" not in _actions(rejecting, fact_id)


def test_auto_accept_cannot_overwrite_a_reject_that_lands_mid_transition(tmp_path):
    """The rule's own version of the cross-connection race.

    The pass-level test below hands `auto_accept_fact` a fact that is already
    superseded by the time it is called, so the tier pre-check alone turns it
    away and the conditional write never has to do anything. This test opens the
    window the pre-check cannot see: the reject lands *after* the tier read, on
    another connection, where `self._lock` has no say. Only the status condition
    on the UPDATE stops the rule overwriting a human's rejection here.
    """
    db = tmp_path / "kb.sqlite"
    accepting = _store_at(db)
    rejecting = _store_at(db)
    fact_id = accepting.add_fact("Report", "author", "Kim", status="needs_review")

    real_get_fact = accepting.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved:
            interleaved.append(True)
            # The other connection's human presses reject in this window.
            rejecting.reject_fact(fact_id)
        return row

    accepting.get_fact = reject_once_after_the_read
    row = accepting.auto_accept_fact(fact_id, rule_name="corroborated_no_conflict")

    assert interleaved, "the reject never landed — the test proves nothing"
    # The rejection is terminal: no rule may take a fact out of superseded.
    assert rejecting.get_fact(fact_id)["status"] == "superseded"
    # None is how the caller learns to skip both `applied` and the audit event.
    assert row is None
    assert "auto_accepted" not in _actions(rejecting, fact_id)


def test_reject_cannot_overwrite_a_reject_that_lands_mid_transition(tmp_path):
    """The same window again, in `reject_fact` — the one write of the four that
    still landed unconditionally.

    Reject's own pre-check ("is it already superseded?") is a read, so a reject
    arriving on another connection lands after it just as it does for accept and
    toggle. The second write agrees about the *status*, so no state is corrupted
    here — but it still reports `changed=True` for a transition it did not make,
    which is exactly the claim the route turns into an auto-accept pass, and it
    files a second `rejected` row for one human decision.
    """
    db = tmp_path / "kb.sqlite"
    rejecting = _store_at(db)
    other = _store_at(db)
    fact_id = rejecting.add_fact("Report", "author", "Kim", status="needs_review")

    real_get_fact = rejecting.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved:
            interleaved.append(True)
            # The other connection's human presses reject in this window.
            other.reject_fact(fact_id)
        return row

    rejecting.get_fact = reject_once_after_the_read
    decision = rejecting.reject_fact(fact_id)

    assert interleaved, "the reject never landed — the test proves nothing"
    assert other.get_fact(fact_id)["status"] == "superseded"
    assert decision.fact["status"] == "superseded"
    # The loser reports its loss, so the route above it skips the follow-on pass.
    assert decision.changed is False
    # One human decision, one audit row — the loser wrote nothing to log.
    assert _actions(other, fact_id).count("rejected") == 1


def test_a_reject_that_loses_the_race_does_not_run_auto_accept(tmp_path):
    """The route-level consequence: the losing reject decided nothing, so the
    rule must not act on a transition that already belonged to someone else."""
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="needs_review")
    other = _store_at(tmp_path / "kb.sqlite")

    real_get_fact = store.get_fact
    interleaved = []

    def reject_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved and target_id == target:
            interleaved.append(True)
            other.reject_fact(target)
        return row

    store.get_fact = reject_once_after_the_read
    try:
        resp = client.post(f"/facts/{target}/reject")
    finally:
        store.get_fact = real_get_fact

    assert interleaved, "the reject never landed — the test proves nothing"
    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "superseded"
    assert _actions(store, target).count("rejected") == 1
    _assert_untouched(store, eligible, resp)


def test_auto_accept_cannot_overwrite_a_reject_that_lands_after_the_snapshot(tmp_path):
    """Auto-accept recommends from a snapshot; a human can reject in the gap
    between that snapshot and the write. The write must re-check, not assume."""
    client, store = _auto_accept_client(tmp_path)
    acted, sibling = _corroborated_pair(
        store, acted_status="needs_review", sibling_status="confirmed"
    )

    real_recommendations = acceptance.accept_recommendations
    rejected_in_the_gap = []

    def reject_after_recommending(target_store):
        recommendations = real_recommendations(target_store)
        if not rejected_in_the_gap:
            rejected_in_the_gap.append(True)
            # The human rejects while the rule is still holding a stale snapshot
            # that says "eligible".
            target_store.reject_fact(acted)
        return recommendations

    acceptance.accept_recommendations = reject_after_recommending
    try:
        applied = acceptance.apply_auto_accept_recommendations(store)
    finally:
        acceptance.accept_recommendations = real_recommendations

    assert rejected_in_the_gap, "the reject never landed — the test proves nothing"
    # superseded is terminal: no rule may take a fact out of it.
    assert store.get_fact(acted)["status"] == "superseded"
    assert acted not in [rec.fact_id for rec in applied]
    # The refused promotion leaves no audit trail claiming it happened.
    assert "auto_accepted" not in _actions(store, acted)
    assert not [
        e for e in store.fact_events(acted) if e["event_type"] == "auto_accept_applied"
    ]
    assert store.get_fact(sibling)["status"] == "confirmed"


def test_auto_accept_still_promotes_an_eligible_fact(tmp_path):
    # Regression guard: the re-check must not block the promotions auto-accept
    # exists to make.
    client, store = _auto_accept_client(tmp_path)
    acted, _sibling = _corroborated_pair(
        store, acted_status="needs_review", sibling_status="confirmed"
    )

    applied = acceptance.apply_auto_accept_recommendations(store)

    assert [rec.fact_id for rec in applied] == [acted]
    assert store.get_fact(acted)["status"] == "accepted"
    assert "auto_accepted" in _actions(store, acted)


# --- the other two routes that hand facts to the rule ---------------------


def test_amend_lets_the_rule_promote_the_amended_fact(tmp_path):
    """An amend decides the fact's content, not its tier, so the rule may act on
    the amended fact itself — the opposite of a toggle demotion.

    This is that distinction as a worked example: the fact's object is a typo
    that keeps it from matching the second source. Correcting it *is*
    corroboration arriving, so promoting on it is the recommender working, not a
    decision being undone.
    """
    client, store = _auto_accept_client(tmp_path)
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = _done_job(store, source_a)
    job_b = _done_job(store, source_b)
    acted = store.add_fact(
        "Report", "author", "Kimm",  # the typo: no support while it stands
        status="needs_review", source_id=source_a, job_id=job_a,
    )
    store.add_fact(
        "Report", "author", "Kim",
        status="confirmed", source_id=source_b, job_id=job_b,
    )
    # Nothing is eligible yet: the typo means the two sources disagree.
    assert store.get_fact(acted)["status"] == "needs_review"

    resp = client.post(
        f"/facts/{acted}/amend",
        data={"subject": "Report", "relation": "author", "object": "Kim"},
    )

    assert resp.status_code == 200
    assert store.get_fact(acted)["object"] == "Kim"
    # The correction brought the second source's support with it.
    assert store.get_fact(acted)["status"] == "accepted"
    assert "auto_accepted" in _actions(store, acted)


def test_accept_all_promotes_a_corroborated_fact_in_another_source(tmp_path):
    """Bulk-confirming one source is a decision too, and it corroborates facts
    outside that source — so it hands them to the rule like the single-fact
    routes do."""
    client, store = _auto_accept_client(tmp_path)
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = _done_job(store, source_a)
    job_b = _done_job(store, source_b)
    bulk = store.add_fact(
        "Report", "author", "Kim",
        status="needs_review", source_id=source_a, job_id=job_a,
    )
    elsewhere = store.add_fact(
        "Report", "author", "Kim",
        status="needs_review", source_id=source_b, job_id=job_b,
    )

    resp = client.post(f"/sources/{source_a}/accept-all")

    assert resp.status_code == 200  # 303 followed to /sources
    assert store.get_fact(bulk)["status"] == "confirmed"
    # The fact in the *other* source was corroborated by the bulk accept, and
    # the route runs the rule so it doesn't sit in the queue until the next job.
    assert store.get_fact(elsewhere)["status"] == "accepted"
    assert "auto_accepted" in _actions(store, elsewhere)


# --- #263 review 2: a no-op / replayed POST is not a review decision ---------


def _eligible_pair(store: Store):
    """One confirmed fact and one review-tier fact stating the same triple from
    two analysed sources: the review-tier one is already auto-accept eligible.

    It is the tripwire for these tests — if a route runs the rule at all, this
    fact moves to `accepted`.
    """
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    job_a = _done_job(store, source_a)
    job_b = _done_job(store, source_b)
    store.add_fact(
        "Report", "author", "Kim",
        status="confirmed", source_id=source_a, job_id=job_a,
    )
    eligible = store.add_fact(
        "Report", "author", "Kim",
        status="needs_review", source_id=source_b, job_id=job_b,
    )
    return eligible


def _auto_accept_events(store: Store, fact_id: int) -> list:
    return [
        e for e in store.fact_events(fact_id) if e["event_type"] == "auto_accept_applied"
    ]


def _assert_untouched(store: Store, eligible: int, resp) -> None:
    assert store.get_fact(eligible)["status"] == "needs_review"
    assert "auto_accepted" not in _actions(store, eligible)
    assert not _auto_accept_events(store, eligible)
    assert "HX-Refresh" not in resp.headers


def test_replayed_accept_does_not_run_auto_accept(tmp_path):
    # A second /accept on an already-confirmed fact writes nothing: the human
    # made no new decision in this request, so nothing may be promoted off it.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="confirmed")

    resp = client.post(f"/facts/{target}/accept")

    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "confirmed"
    _assert_untouched(store, eligible, resp)


def test_accept_on_a_superseded_fact_does_not_run_auto_accept(tmp_path):
    # Accept refuses to revive a rejection, so it is a no-op here too.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="superseded")

    resp = client.post(f"/facts/{target}/accept")

    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "superseded"
    _assert_untouched(store, eligible, resp)


def test_replayed_reject_does_not_run_auto_accept(tmp_path):
    # Reject is terminal and idempotent; the replay decides nothing.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="superseded")

    resp = client.post(f"/facts/{target}/reject")

    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "superseded"
    _assert_untouched(store, eligible, resp)


def test_toggle_on_a_superseded_fact_does_not_run_auto_accept(tmp_path):
    # A toggle that hits the no-revival guard writes nothing either.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="superseded")

    resp = client.post(f"/facts/{target}/toggle")

    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "superseded"
    _assert_untouched(store, eligible, resp)


def test_missing_amend_target_does_not_run_auto_accept(tmp_path):
    # A stale inline-edit POST for a fact that no longer exists writes nothing,
    # so it must not become an excuse to promote unrelated eligible facts.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)

    resp = client.post(
        "/facts/999999/amend",
        data={"subject": "Ledger", "relation": "owner", "object": "Park"},
    )

    assert resp.status_code == 200
    _assert_untouched(store, eligible, resp)


def test_replayed_amend_does_not_run_auto_accept(tmp_path):
    # Reposting the same content is not a new human decision. It must not log an
    # amendment, rewrite timestamps, or run the auto-accept pass.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact(
        "Ledger", "owner", "Park", status="needs_review", note="unchanged"
    )
    before = dict(store.get_fact(target))

    resp = client.post(
        f"/facts/{target}/amend",
        data={
            "subject": "Ledger",
            "relation": "owner",
            "object": "Park",
            "note": "unchanged",
        },
    )

    assert resp.status_code == 200
    assert dict(store.get_fact(target)) == before
    assert "amended" not in _actions(store, target)
    _assert_untouched(store, eligible, resp)


def test_accept_all_on_a_source_with_nothing_to_confirm_does_not_run_auto_accept(
    tmp_path,
):
    # The bulk route carries the same contract: no fact was confirmed, so no
    # decision was made and the rule has nothing new to act on.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    empty_source = store.add_source("sources/empty.txt")

    resp = client.post(f"/sources/{empty_source}/accept-all")

    assert resp.status_code == 200  # 303 followed to /sources
    assert store.get_fact(eligible)["status"] == "needs_review"
    assert "auto_accepted" not in _actions(store, eligible)
    assert not _auto_accept_events(store, eligible)


def test_reject_that_really_transitions_still_runs_auto_accept(tmp_path):
    # Over-fix guard: only no-ops are filtered. A reject that actually moves a
    # fact is a decision, and the rule still runs for the siblings it unblocks.
    client, store = _auto_accept_client(tmp_path)
    eligible = _eligible_pair(store)
    target = store.add_fact("Ledger", "owner", "Park", status="needs_review")

    resp = client.post(f"/facts/{target}/reject")

    assert resp.status_code == 200
    assert store.get_fact(target)["status"] == "superseded"
    assert store.get_fact(eligible)["status"] == "accepted"
    assert resp.headers.get("HX-Refresh") == "true"


# --- #264: guarded auto-retract of rule-accepted facts --------------------


def test_auto_retract_demotes_a_rule_accepted_fact(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("sources/a.txt")
    fact_id = s.add_fact(
        "Report", "author", "Kim", status="accepted", source_id=source_id
    )

    after = s.auto_retract_fact(fact_id, rule_name="corroborated_no_conflict")

    # Leaves the engine tier, back into the human review queue.
    assert after is not None
    assert after["status"] == "needs_review"
    assert s.get_fact(fact_id)["status"] == "needs_review"
    # Dual audit write: a review_log action and a rule-actor fact_event.
    assert "auto_retracted" in _actions(s, fact_id)
    events = [e for e in s.fact_events(fact_id) if e["event_type"] == "auto_retracted"]
    assert len(events) == 1
    assert events[0]["actor"] == "rule"
    assert events[0]["rule_name"] == "corroborated_no_conflict"


def test_auto_retract_refuses_a_confirmed_fact(tmp_path):
    # A human ratification (confirmed) outranks the rule — untouchable.
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status="confirmed")

    assert s.auto_retract_fact(fact_id, rule_name="corroborated_no_conflict") is None
    assert s.get_fact(fact_id)["status"] == "confirmed"
    assert "auto_retracted" not in _actions(s, fact_id)


@pytest.mark.parametrize("status", ["superseded", "candidate", "needs_review"])
def test_auto_retract_refuses_non_accepted_statuses(tmp_path, status):
    s = _store(tmp_path)
    fact_id = s.add_fact("Report", "author", "Kim", status=status)

    assert s.auto_retract_fact(fact_id, rule_name="corroborated_no_conflict") is None
    assert s.get_fact(fact_id)["status"] == status
    assert "auto_retracted" not in _actions(s, fact_id)


def test_auto_retract_missing_fact_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.auto_retract_fact(9999, rule_name="corroborated_no_conflict") is None


def test_auto_retract_skips_when_the_fact_leaves_accepted_mid_transition(tmp_path):
    """The cross-connection race, mirroring the auto_accept version.

    The competing decision lands on another connection *after* the tier read,
    where this instance's lock has no say — only the status condition on the
    UPDATE stops the rule demoting a row that has already moved on.
    """
    db = tmp_path / "kb.sqlite"
    retracting = _store_at(db)
    other = _store_at(db)
    fact_id = retracting.add_fact("Report", "author", "Kim", status="accepted")

    real_get_fact = retracting.get_fact
    interleaved = []

    def move_once_after_the_read(target_id: int):
        row = real_get_fact(target_id)
        if not interleaved:
            interleaved.append(True)
            # Another connection supersedes the fact inside the window.
            other.reject_fact(fact_id)
        return row

    retracting.get_fact = move_once_after_the_read
    row = retracting.auto_retract_fact(fact_id, rule_name="corroborated_no_conflict")

    assert interleaved, "the competing decision never landed — the test proves nothing"
    assert row is None
    assert other.get_fact(fact_id)["status"] == "superseded"
    assert "auto_retracted" not in _actions(other, fact_id)


# --- #292: superseded stays superseded, whichever public route you take ---

# Every public Store method that writes `facts.status`. Membership is not a
# matter of taste: the scan below reads Store's own source and fails if a status
# writer lands outside this set, so a new blind write cannot join the class by
# being left off a list. `amend_fact` is absent because it rewrites content and
# never touches status; `add_fact(status=...)` is absent because it picks a
# fact's *starting* status rather than moving an existing one.
FACT_TRANSITION_METHODS = frozenset(
    {
        "accept_fact",
        "accept_review_facts_for_source",
        "auto_accept_fact",
        "reject_fact",
        "toggle_review",
    }
)

# The assignment list of every `UPDATE facts SET ... WHERE`. Matching the whole
# SET clause rather than stopping at the first quote is what makes column order
# irrelevant: `datetime('now')` sits between `SET` and `status` the moment
# someone writes `updated_at` first, and a quote-sensitive pattern would read
# that as "no status write" and wave the row straight past both guards.
_STATUS_WRITE = re.compile(r"UPDATE\s+facts\s+SET(.*?)WHERE", re.IGNORECASE)


def _public_methods(cls) -> frozenset[str]:
    return frozenset(
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name))
    )


def _writes_status(method) -> bool:
    source = " ".join(inspect.getsource(method).split())
    return any("status" in clause for clause in _STATUS_WRITE.findall(source))


def _status_writers(cls) -> frozenset[str]:
    return frozenset(
        name for name in _public_methods(cls) if _writes_status(getattr(cls, name))
    )


def test_every_status_writer_is_a_swept_transition():
    # The one that matters: a new method writing facts.status is red until it
    # joins the sweep below, so an unguarded write cannot land quietly. This
    # also subsumes the structural check on `set_status` (#292) — reintroducing
    # it, or anything shaped like it, shows up here as an unswept writer.
    assert _status_writers(Store) <= FACT_TRANSITION_METHODS


def test_no_public_transition_revives_a_superseded_fact(tmp_path):
    store = _store(tmp_path)
    source_id = store.add_source("sources/rejected.txt")
    fact_id = store.add_fact(
        "Ledger", "owner", "Park", status="needs_review", source_id=source_id
    )
    store.reject_fact(fact_id)
    assert store.get_fact(fact_id)["status"] == "superseded"
    log_before = _actions(store, fact_id)

    routes = {
        "accept_fact": lambda: store.accept_fact(fact_id),
        "accept_review_facts_for_source": lambda: store.accept_review_facts_for_source(
            source_id
        ),
        "auto_accept_fact": lambda: store.auto_accept_fact(fact_id, rule_name="r"),
        "reject_fact": lambda: store.reject_fact(fact_id),
        "toggle_review": lambda: store.toggle_review(fact_id),
    }
    assert set(routes) == FACT_TRANSITION_METHODS

    for name, call in routes.items():
        call()
        # Not just the status: a route that declined to move a fact must not
        # claim the decision in the audit log either.
        assert store.get_fact(fact_id)["status"] == "superseded", name
        assert _actions(store, fact_id) == log_before, name
