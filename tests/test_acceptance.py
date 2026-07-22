# SPDX-License-Identifier: MPL-2.0
import json
import pathlib
import re

from verinote.pipeline.acceptance import (
    accept_recommendation,
    apply_auto_accept_recommendations,
    RULE_NAME,
)
from verinote.pipeline.corroboration import store_single_valued_conflicts
from verinote.store import Store, engine_statuses


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _write_policy(tmp_path) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\n'
        'functional("published_year").\n'
        'functional("revenue").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `sales` -> `revenue`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar\n",
        encoding="utf-8",
    )


def _done_job(store: Store, source_id: int) -> int:
    job_id = store.create_extraction_job(
        source_id=source_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id,
        source_id=source_id,
        chunks=["Sample body"],
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_done(chunk_id, candidates=1)
    store.finish_extraction_job(job_id)
    return job_id


def _corroborated_candidate(store, subject, relation, value, source_paths):
    """Add one candidate per path (each a distinct done-job source) for one value.

    Returns the first candidate's id. With two paths the value is corroborated
    (two distinct sources); with one path it is a weakly-sourced single witness.
    """
    first_id = None
    for path in source_paths:
        source_id = store.add_source(path)
        job_id = _done_job(store, source_id)
        fact_id = store.add_fact(
            subject,
            relation,
            value,
            status="candidate",
            source_id=source_id,
            job_id=job_id,
        )
        if first_id is None:
            first_id = fact_id
    return first_id


def test_accept_recommendation_requires_distinct_completed_source_support(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    job_a = _done_job(s, source_a)
    job_b = _done_job(s, source_b)
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        confidence=0.01,
        source_id=source_a,
        job_id=job_a,
    )
    support_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        confidence=0.99,
        source_id=source_b,
        job_id=job_b,
    )

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is True
    assert recommendation.reasons == ()
    assert recommendation.support_sources == ("sources/a.txt", "sources/b.txt")
    assert recommendation.support_fact_ids == (fact_id, support_id)


def test_accept_recommendation_rejects_same_source_duplicates(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_id = s.add_source("sources/a.txt")
    job_id = _done_job(s, source_id)
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_id,
        job_id=job_id,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_id,
        job_id=job_id,
    )

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "insufficient_distinct_source_support" in recommendation.reasons


def test_accept_recommendation_rejects_incomplete_source_analysis(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    job_b = _done_job(s, source_b)
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_b,
        job_id=job_b,
    )

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "source_analysis_incomplete" in recommendation.reasons


def test_accept_recommendation_rejects_single_valued_conflicts(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    source_c = s.add_source("sources/c.txt")
    job_a = _done_job(s, source_a)
    job_b = _done_job(s, source_b)
    job_c = _done_job(s, source_c)
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_a,
        job_id=job_a,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_b,
        job_id=job_b,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2025",
        status="confirmed",
        source_id=source_c,
        job_id=job_c,
    )

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "single_valued_conflict" in recommendation.reasons


def test_accept_recommendation_uses_alias_and_typed_scalar_normalization(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    job_a = _done_job(s, source_a)
    job_b = _done_job(s, source_b)
    fact_id = s.add_fact(
        "Sample Company",
        "sales",
        'amount(5400,"억")',
        status="candidate",
        source_id=source_a,
        job_id=job_a,
    )
    s.add_fact(
        "Sample Company",
        "revenue",
        'amount(0.54,"조")',
        status="confirmed",
        source_id=source_b,
        job_id=job_b,
    )

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is True
    assert recommendation.canonical_relation == "revenue"
    assert recommendation.typed_normalization == "revenue_scalar=540000000000"


def test_accept_recommendation_rejects_previous_rejection(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_id = s.add_source("sources/a.txt")
    job_id = _done_job(s, source_id)
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_id,
        job_id=job_id,
    )
    s.reject_fact(fact_id)

    recommendation = accept_recommendation(s, fact_id)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "previously_rejected_or_superseded" in recommendation.reasons


def test_apply_auto_accept_promotes_only_eligible_facts_and_records_rule_event(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    job_a = _done_job(s, source_a)
    job_b = _done_job(s, source_b)
    eligible = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_a,
        job_id=job_a,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_b,
        job_id=job_b,
    )
    ineligible = s.add_fact(
        "Unsupported Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=source_a,
        job_id=job_a,
    )

    applied = apply_auto_accept_recommendations(s)

    assert [recommendation.fact_id for recommendation in applied] == [eligible]
    assert s.get_fact(eligible)["status"] == "accepted"
    assert s.get_fact(ineligible)["status"] == "candidate"
    events = s.fact_events(eligible)
    assert [event["event_type"] for event in events][-2:] == [
        "auto_accepted",
        "auto_accept_applied",
    ]
    assert events[-1]["actor"] == "rule"
    assert events[-1]["rule_name"] == RULE_NAME


def test_auto_accept_never_promotes_two_conflicting_values_in_one_batch(tmp_path):
    # Issue #287 done-criterion: two contradictory values on a functional
    # relation, each corroborated by two distinct done-job sources, all
    # candidates. Neither may auto-promote — they mutually block, order-free.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    year_2024 = _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    year_2025 = _corroborated_candidate(
        s, "Sample Report", "published_year", "2025",
        ["sources/b1.txt", "sources/b2.txt"],
    )

    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    assert s.get_fact(year_2024)["status"] == "candidate"
    assert s.get_fact(year_2025)["status"] == "candidate"


def test_auto_accept_never_leaves_the_kb_failing_its_own_policy(tmp_path):
    # Issue #287 done-criterion: after auto-accept runs, the KB must not hold two
    # accepted values for a single-valued relation — its own functional_conflict
    # policy would flag exactly that.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    _corroborated_candidate(
        s, "Sample Report", "published_year", "2025",
        ["sources/b1.txt", "sources/b2.txt"],
    )

    apply_auto_accept_recommendations(s)

    assert store_single_valued_conflicts(s) == []


def test_accept_recommendation_flags_conflict_from_rival_candidate(tmp_path):
    # The new arm: a corroborated CANDIDATE rival (not just an engine-tier fact)
    # now blocks the target directly.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    target = _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    _corroborated_candidate(
        s, "Sample Report", "published_year", "2025",
        ["sources/b1.txt", "sources/b2.txt"],
    )

    recommendation = accept_recommendation(s, target)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "single_valued_conflict" in recommendation.reasons


def test_accept_recommendation_weakly_sourced_rival_candidate_still_blocks(tmp_path):
    # Ratified conservatism: even a single-source rival candidate withholds the
    # target — the rule refuses to adjudicate any contested value.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    target = _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    _corroborated_candidate(
        s, "Sample Report", "published_year", "2025", ["sources/b1.txt"]
    )

    recommendation = accept_recommendation(s, target)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "single_valued_conflict" in recommendation.reasons


def test_accept_recommendation_rival_without_source_does_not_block(tmp_path):
    # The `fact.source` conjunct: a rival value with no source cannot witness, so
    # it does not block. The target stays eligible.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    target = _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    s.add_fact("Sample Report", "published_year", "2025", status="candidate")

    recommendation = accept_recommendation(s, target)

    assert recommendation is not None
    assert recommendation.eligible is True
    assert "single_valued_conflict" not in recommendation.reasons


def test_rejecting_one_rival_unblocks_auto_accept_of_the_other(tmp_path):
    # Resolution / starvation guard: while both stand, both are withheld. A human
    # rejecting the wrong value supersedes it (it witnesses on neither tier), and
    # the survivor auto-promotes on the next apply. The loser is a single row so
    # one reject_fact removes the whole contested value.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    survivor = _corroborated_candidate(
        s, "Sample Report", "published_year", "2024",
        ["sources/a1.txt", "sources/a2.txt"],
    )
    loser = _corroborated_candidate(
        s, "Sample Report", "published_year", "2025", ["sources/b1.txt"]
    )

    assert apply_auto_accept_recommendations(s) == []

    s.reject_fact(loser)
    applied = apply_auto_accept_recommendations(s)

    # The survivor value promotes (both its corroborating rows are same-valued, so
    # promoting both is no conflict); the rejected loser is not resurrected.
    applied_ids = {recommendation.fact_id for recommendation in applied}
    assert survivor in applied_ids
    assert loser not in applied_ids
    assert s.get_fact(survivor)["status"] == "accepted"
    assert s.get_fact(loser)["status"] == "superseded"
    assert store_single_valued_conflicts(s) == []


# --- #264: retract rule-accepted facts whose corroboration basis lapsed ----


def _accepted_ids(store):
    return [f["id"] for f in store.facts(statuses=["accepted"])]


def test_apply_retracts_a_rule_accepted_fact_after_its_basis_lapses(tmp_path):
    # #264 headline: two same-triple corroborated candidates auto-accept; a human
    # rejects one source's row; the value now has a single source and must return
    # to review (out of the engine tier), with an auditable retraction event.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    survivor = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)
    accepted = _accepted_ids(s)
    assert len(accepted) == 2 and survivor in accepted

    other = next(fact_id for fact_id in accepted if fact_id != survivor)
    s.reject_fact(other)

    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    assert s.get_fact(survivor)["status"] == "needs_review"
    assert survivor not in [f["id"] for f in s.facts(statuses=engine_statuses())]
    events = [
        e for e in s.fact_events(survivor) if e["event_type"] == "auto_accept_retracted"
    ]
    assert len(events) == 1
    assert events[0]["actor"] == "rule"
    assert events[0]["rule_name"] == RULE_NAME
    payload = json.loads(events[0]["after_json"])
    assert payload["reasons"] == ["insufficient_distinct_source_support"]
    assert payload["remaining_support_sources"] == ["sources/a.txt"]
    assert payload["remaining_support_fact_ids"] == [survivor]
    assert payload["canonical_relation"] == "published_year"


def test_repeated_apply_passes_do_not_rethrash_a_retracted_fact(tmp_path):
    # Idempotence: retraction and promotion share the one `< 2` threshold, so a
    # retracted fact is neither re-promoted (its support is still short, so
    # recommend() marks insufficient_distinct_source_support) nor re-retracted
    # (its snapshot status is needs_review, not accepted) — however many passes
    # run. This pins the commit's anti-oscillation claim in both directions.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    survivor = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)
    other = next(fact_id for fact_id in _accepted_ids(s) if fact_id != survivor)
    s.reject_fact(other)
    apply_auto_accept_recommendations(s)
    assert s.get_fact(survivor)["status"] == "needs_review"

    assert apply_auto_accept_recommendations(s) == []
    assert apply_auto_accept_recommendations(s) == []

    assert s.get_fact(survivor)["status"] == "needs_review"
    assert (
        len(
            [
                e
                for e in s.fact_events(survivor)
                if e["event_type"] == "auto_accept_retracted"
            ]
        )
        == 1
    )


def test_apply_does_not_retract_a_human_confirmed_fact(tmp_path):
    # A confirmed fact is a human ratification; a lapsed basis never retracts it.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_id = s.add_source("sources/a.txt")
    job_id = _done_job(s, source_id)
    fact_id = s.add_fact(
        "Report", "published_year", "2024",
        status="confirmed", source_id=source_id, job_id=job_id,
    )

    apply_auto_accept_recommendations(s)

    assert s.get_fact(fact_id)["status"] == "confirmed"
    assert [
        e for e in s.fact_events(fact_id) if e["event_type"] == "auto_accept_retracted"
    ] == []


def test_apply_leaves_a_still_corroborated_accepted_fact_untouched(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)

    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    accepted = s.facts(statuses=["accepted"])
    assert len(accepted) == 2
    for fact in accepted:
        assert [
            e for e in s.fact_events(fact["id"])
            if e["event_type"] == "auto_accept_retracted"
        ] == []


def test_apply_does_not_retract_when_a_same_triple_reextraction_is_still_running(tmp_path):
    # No oscillation: the count is job-independent, so a fresh same-triple
    # candidate whose job is still RUNNING never triggers retraction. A job-based
    # trigger would flag the accepted value as incomplete and yank it mid-analysis.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    accepted = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)

    source_c = s.add_source("sources/c.txt")
    running_job = s.create_extraction_job(
        source_id=source_c, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(
        job_id=running_job, source_id=source_c, chunks=["body"]
    )[0]
    s.mark_extraction_job_running(running_job)
    s.mark_chunk_running(chunk_id)
    s.add_fact(
        "Report", "published_year", "2024",
        status="candidate", source_id=source_c, job_id=running_job,
    )

    apply_auto_accept_recommendations(s)

    assert s.get_fact(accepted)["status"] == "accepted"
    assert [
        e for e in s.fact_events(accepted) if e["event_type"] == "auto_accept_retracted"
    ] == []


def test_apply_does_not_retract_an_accepted_fact_over_a_rival_candidate(tmp_path):
    # Retraction is basis-only, never conflict-based: a new weakly-sourced rival
    # candidate is withheld from promotion (#287) but must not yank the accepted
    # value, whose own two sources still stand.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    accepted = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)
    _corroborated_candidate(s, "Report", "published_year", "2025", ["sources/c.txt"])

    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    assert s.get_fact(accepted)["status"] == "accepted"
    accepted_rows = s.facts(statuses=["accepted"])
    assert {f["object"] for f in accepted_rows} == {"2024"}
    for fact in accepted_rows:
        assert [
            e for e in s.fact_events(fact["id"])
            if e["event_type"] == "auto_accept_retracted"
        ] == []


def test_apply_does_not_retract_an_excluded_fact(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    survivor = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)
    other = next(fact_id for fact_id in _accepted_ids(s) if fact_id != survivor)
    s.reject_fact(other)

    apply_auto_accept_recommendations(s, exclude_fact_ids=[survivor])

    assert s.get_fact(survivor)["status"] == "accepted"
    assert [
        e for e in s.fact_events(survivor) if e["event_type"] == "auto_accept_retracted"
    ] == []


def test_apply_retract_and_rival_promotion_do_not_race_in_one_pass(tmp_path):
    # One-snapshot determinism: X is retracted (basis lapsed) but its review-tier
    # rival Y is NOT promoted in the same pass — the snapshot still shows X
    # accepted, so Y stays blocked (#287); Y's promotion is deferred to the next
    # cascade.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_x = s.add_source("sources/x.txt")
    job_x = _done_job(s, source_x)
    x = s.add_fact(
        "Report", "published_year", "2024",
        status="accepted", source_id=source_x, job_id=job_x,
    )
    y = _corroborated_candidate(
        s, "Report", "published_year", "2025", ["sources/y1.txt", "sources/y2.txt"]
    )

    applied = apply_auto_accept_recommendations(s)

    assert [recommendation.fact_id for recommendation in applied] == []
    assert s.get_fact(x)["status"] == "needs_review"
    assert s.get_fact(y)["status"] == "candidate"


def test_apply_retracts_an_accepted_fact_amended_to_a_new_value(tmp_path):
    # Amend-of-accepted: changing the object moves the fact off the triple its
    # corroborators support, so its basis lapses and the next pass retracts it.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    _corroborated_candidate(
        s, "Report", "published_year", "2024",
        ["sources/a.txt", "sources/b.txt", "sources/c.txt"],
    )
    apply_auto_accept_recommendations(s)
    accepted = s.facts(statuses=["accepted"])
    assert len(accepted) == 3
    amended = accepted[0]["id"]
    s.amend_fact(amended, subject="Report", relation="published_year", obj="2099")
    assert s.get_fact(amended)["status"] == "accepted"

    apply_auto_accept_recommendations(s)

    assert s.get_fact(amended)["status"] == "needs_review"
    assert [
        e for e in s.fact_events(amended) if e["event_type"] == "auto_accept_retracted"
    ] != []
    survivors = s.facts(statuses=["accepted"])
    assert len(survivors) == 2
    assert {f["object"] for f in survivors} == {"2024"}


def test_only_auto_accept_fact_writes_the_accepted_status():
    """STATIC SOURCE CHECK, not a proof (invariant guard until #292).

    auto_retract_fact relies on `accepted` meaning "rule-promoted, not
    human-ratified", which holds only while exactly one production path writes
    that status — auto_accept_fact — and no set_status call passes it.

    This greps the source for the realistic regression: a literal
    `SET status = 'accepted'` added elsewhere, or a literal
    `set_status(..., 'accepted')`. It CANNOT see an indirect write such as
    `set_status(fact_id, some_var)` where the variable happens to hold
    "accepted", because that is invisible to a text scan. Retiring set_status —
    which is what would make the invariant structural rather than conventional —
    is #292's scope. Do not read a pass here as a guarantee.
    """
    verinote_root = pathlib.Path(__file__).resolve().parent.parent / "verinote"
    writes = []
    set_status_accepted = re.compile(r"set_status\([^)]*accepted")
    for path in sorted(verinote_root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if "SET status = 'accepted'" in line:
                writes.append((path, lineno, lines))
        assert not set_status_accepted.search("\n".join(lines)), (
            f"{path} calls set_status with 'accepted'"
        )

    assert len(writes) == 1, f"unexpected accepted-status writes: {[w[:2] for w in writes]}"
    path, lineno, lines = writes[0]
    assert path.name == "db.py"
    preceding_defs = [line for line in lines[:lineno] if line.lstrip().startswith("def ")]
    assert preceding_defs[-1].strip().startswith("def auto_accept_fact(")


# --- #329: stale facts are witness-ineligible until their source supports them --


def _mark_stale(store, fact_id):
    """Demote a fact exactly as Unit 3's sweep eventually will: needs_review + stale."""
    store._conn.execute(
        "UPDATE facts SET status = 'needs_review', stale = 1 WHERE id = ?", (fact_id,)
    )


def test_stale_fact_is_never_re_promoted_even_with_other_live_witnesses(tmp_path):
    # #329 Gap 1: a stale fact must not re-promote ITSELF however many live
    # witnesses its value keeps. It is excluded from its own support set, yet two
    # other distinct sources remain -- so only recommend()'s own stale guard blocks
    # it; the _supporting_facts exclusion alone would leak here.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    stale_fact = _corroborated_candidate(
        s,
        "Report",
        "published_year",
        "2024",
        ["sources/a.txt", "sources/b.txt", "sources/c.txt"],
    )
    _mark_stale(s, stale_fact)

    recommendation = accept_recommendation(s, stale_fact)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "stale_citation" in recommendation.reasons
    # Two other live, distinct-source witnesses remain: the block is the stale
    # guard's doing, not a shortage of corroboration.
    assert len(recommendation.support_sources) == 2

    apply_auto_accept_recommendations(s)
    assert s.get_fact(stale_fact)["status"] == "needs_review"
    assert stale_fact not in [f["id"] for f in s.facts(statuses=engine_statuses())]


def test_a_stale_witness_does_not_corroborate_another_fact(tmp_path):
    # #329: a live candidate whose value is witnessed only by {stale A, itself}
    # loses its corroboration once A is excluded, dropping to a lone source.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    stale_fact = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    live_fact = next(f["id"] for f in s.facts() if f["id"] != stale_fact)
    _mark_stale(s, stale_fact)

    recommendation = accept_recommendation(s, live_fact)

    assert recommendation is not None
    assert recommendation.eligible is False
    assert "insufficient_distinct_source_support" in recommendation.reasons
    # Only the live fact's own source is left standing.
    assert recommendation.support_sources == ("sources/b.txt",)


def test_a_stale_witness_lapses_a_dependent_accepted_facts_basis(tmp_path):
    # #329 x #264, an intentional cascade (NOT a regression): two corroborated
    # candidates auto-accept; one is then demoted stale, so the survivor's live
    # distinct-source count drops below two and #264's existing machinery retracts
    # it to needs_review.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    survivor = _corroborated_candidate(
        s, "Report", "published_year", "2024", ["sources/a.txt", "sources/b.txt"]
    )
    apply_auto_accept_recommendations(s)
    accepted = _accepted_ids(s)
    assert len(accepted) == 2 and survivor in accepted
    other = next(fact_id for fact_id in accepted if fact_id != survivor)

    _mark_stale(s, other)
    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    assert s.get_fact(survivor)["status"] == "needs_review"
    events = [
        e for e in s.fact_events(survivor) if e["event_type"] == "auto_accept_retracted"
    ]
    assert len(events) == 1
    assert json.loads(events[0]["after_json"])["reasons"] == [
        "insufficient_distinct_source_support"
    ]


def test_a_stale_bit_is_inert_once_a_fact_leaves_needs_review(tmp_path):
    # The guards key on needs_review specifically, so a human confirm leaves any
    # lingering stale=1 bit inert: the confirmed fact still corroborates normally
    # and supplies the second distinct witness a candidate needs.
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    job_a = _done_job(s, source_a)
    job_b = _done_job(s, source_b)
    confirmed_stale = s.add_fact(
        "Report", "published_year", "2024",
        status="confirmed", source_id=source_a, job_id=job_a,
    )
    s._conn.execute("UPDATE facts SET stale = 1 WHERE id = ?", (confirmed_stale,))
    candidate = s.add_fact(
        "Report", "published_year", "2024",
        status="candidate", source_id=source_b, job_id=job_b,
    )

    recommendation = accept_recommendation(s, candidate)

    assert recommendation is not None
    assert recommendation.eligible is True
    assert recommendation.support_sources == ("sources/a.txt", "sources/b.txt")


def _done_job_at_artifact(store, *, source_id, artifact_id):
    job_id = store.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    store.finish_extraction_job(job_id)
    return job_id


def test_swept_fact_reobserved_then_auto_accepted_via_corroboration(tmp_path):
    # The full cycle the sweep finally makes reachable (#329 x #264, INTENDED, not
    # a defect): the real sweep demotes a confirmed citation as stale; its content
    # later returns and note_fact_reobserved clears the stale bit; because two
    # OTHER live sources still corroborate the triple, #264's auto-accept promotes
    # it straight to `accepted` (not `confirmed` -- a machine trust tier keeps
    # provenance honest) with no human re-click. Asserted to happen, not guarded.
    _write_policy(tmp_path)  # born_in is not functional: no single-valued block
    s = _store(tmp_path)

    src_a = s.add_source("sources/a.txt")
    a_old = s.add_source_artifact(
        source_id=src_a, kind="original_text", path="sources/a-v1.txt", checksum="a1"
    )
    a_new = s.add_source_artifact(
        source_id=src_a, kind="original_text", path="sources/a-v2.txt", checksum="a2"
    )
    a_old_job = _done_job_at_artifact(s, source_id=src_a, artifact_id=a_old)
    a_fact = s.add_fact(
        "Ada", "born_in", "London",
        status="confirmed", source_id=src_a, job_id=a_old_job,
    )
    s.add_fact_evidence(fact_id=a_fact, source_id=src_a, artifact_id=a_old, snippet="London")

    for path in ("sources/b.txt", "sources/c.txt"):
        witness = s.add_source(path)
        job = _done_job(s, witness)
        s.add_fact(
            "Ada", "born_in", "London",
            status="confirmed", source_id=witness, job_id=job,
        )

    # A re-extracts at the new artifact: a Paris candidate anchors there (the whiff
    # guard), and A's London has no anchor at a_new -> the sweep demotes it.
    a_job = _done_job_at_artifact(s, source_id=src_a, artifact_id=a_new)
    paris = s.add_fact(
        "Ada", "born_in", "Paris", status="candidate", source_id=src_a, job_id=a_job
    )
    s.add_fact_evidence(fact_id=paris, source_id=src_a, artifact_id=a_new, snippet="Paris")

    demoted = s.surface_stale_engine_facts(a_job)
    assert [d["id"] for d in demoted] == [a_fact]
    assert s.get_fact(a_fact)["status"] == "needs_review"
    assert s.get_fact(a_fact)["stale"] == 1

    # A's content returns to London: a later clean run re-observes it and clears
    # the stale bit without touching the human's needs_review status.
    a_back = s.add_source_artifact(
        source_id=src_a, kind="original_text", path="sources/a-v3.txt", checksum="a3"
    )
    a_job_back = _done_job_at_artifact(s, source_id=src_a, artifact_id=a_back)
    s.note_fact_reobserved(
        fact_id=a_fact, source_id=src_a, artifact_id=a_back, job_id=a_job_back
    )
    assert s.get_fact(a_fact)["stale"] == 0
    assert s.get_fact(a_fact)["status"] == "needs_review"

    apply_auto_accept_recommendations(s)

    # Witness-eligible again + corroborated by B and C -> promoted to `accepted`.
    assert s.get_fact(a_fact)["status"] == "accepted"


# --- #343: a human rejection must survive a relation-label rename ----------
#
# `reject_fact` supersedes a fact keyed on its LITERAL (subject, relation, object)
# triple, but corroboration/auto-accept eligibility is keyed on the CANONICAL
# relation. A re-extraction that relabels the same value to an aliased-synonym
# relation therefore lands a fresh candidate whose own fact_id carries no rejection
# history, and the two-source auto-accept rule could promote it -- silently
# reversing a human's rejection with no review. The engine now carries a
# source-scoped reject shadow, keyed on the canonical (source, subject, relation,
# value), that blocks the resurrected candidate from BOTH promoting itself (the
# recommend gate) and padding another candidate's witness count (the support filter).


def _write_rename_policy(tmp_path) -> None:
    """A real policy where `sales` and `revenue` canonicalize together (#343).

    Uses the exact alias-file mechanism the suite already exercises, but keeps the
    shared canonical relation `revenue` NON-functional and UNtyped so object_key is
    a plain raw match. These tests then probe only the reject-shadow mechanism, free
    of the #287 single-valued or typed-scalar interactions a functional/typed
    relation would layer on top.
    """
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\n'
        'functional("published_year").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `sales` -> `revenue`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "# no typed relations: revenue stays a raw-string value here\n",
        encoding="utf-8",
    )


def _reconciled_fact(store, *, subject, relation, obj, source_id):
    """Insert one (source, triple) through the REAL reconcile_fact path.

    Each call stands for one extraction run (its own done job), so a second call on
    the same source is a genuine re-extraction. Returns the FactReconcileResult so
    callers can assert whether Store-level dedup matched an existing row or truly
    inserted a fresh one.
    """
    job_id = _done_job(store, source_id)
    return store.reconcile_fact(subject, relation, obj, source_id=source_id, job_id=job_id)


def test_relabeled_reextraction_cannot_silently_reverse_a_rejection(tmp_path):
    # #343 completion criterion. Source A extracts a value under raw label "sales"
    # and a human rejects it (-> superseded). A is re-extracted and the SAME value
    # now carries "revenue" (canonically identical via the alias); the literal-triple
    # dedup misses the superseded row, so a fresh candidate lands. Source B then
    # corroborates the same value. NOTHING may auto-promote: the resurrected
    # candidate is reject-shadowed, so it neither promotes itself nor pads B's count.
    _write_rename_policy(tmp_path)
    s = _store(tmp_path)

    source_a = s.add_source("sources/a.txt")
    f1 = _reconciled_fact(s, subject="Corp", relation="sales", obj="2020", source_id=source_a)
    assert f1.created
    s.reject_fact(f1.fact_id)
    assert s.get_fact(f1.fact_id)["status"] == "superseded"

    # The real dedup genuinely misses the relabeled re-extraction -- proving the
    # gap is at the Store token layer, exactly where #343 locates it.
    f2 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=source_a)
    assert f2.created and f2.fact_id != f1.fact_id

    source_b = s.add_source("sources/b.txt")
    f_b = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=source_b)
    assert f_b.created

    applied = apply_auto_accept_recommendations(s)

    assert applied == []
    assert s.get_fact(f1.fact_id)["status"] == "superseded"
    assert s.get_fact(f2.fact_id)["status"] == "candidate"
    assert s.get_fact(f_b.fact_id)["status"] == "candidate"
    # No engine-tier promotion happened at all.
    assert [f["id"] for f in s.facts(statuses=engine_statuses())] == []


def test_recommend_gate_fires_on_the_resurrected_relabeled_candidate(tmp_path):
    # #343 floor, multi-source: even with two further genuinely-independent sources
    # corroborating (support is plainly sufficient), the resurrected candidate is
    # ineligible -- the reject shadow is the only thing that can hold it, and it does.
    _write_rename_policy(tmp_path)
    s = _store(tmp_path)

    source_a = s.add_source("sources/a.txt")
    f1 = _reconciled_fact(s, subject="Corp", relation="sales", obj="2020", source_id=source_a)
    s.reject_fact(f1.fact_id)
    f2 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=source_a)
    for path in ("sources/b.txt", "sources/c.txt"):
        src = s.add_source(path)
        _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=src)

    rec = accept_recommendation(s, f2.fact_id)

    assert rec is not None
    assert rec.eligible is False
    assert "previously_rejected_or_superseded" in rec.reasons


def test_rejecting_one_value_does_not_shadow_a_different_value(tmp_path):
    # object_key specificity: the shadow is keyed on the VALUE too, so rejecting
    # "2020" must not veto "2021". Source A's own relabeled "2021" witness must keep
    # counting -- were the key only (source, subject, relation) it would be wrongly
    # excluded and "2021" would starve for a second source. It promotes normally.
    _write_rename_policy(tmp_path)
    s = _store(tmp_path)

    source_a = s.add_source("sources/a.txt")
    rejected = _reconciled_fact(s, subject="Corp", relation="sales", obj="2020", source_id=source_a)
    s.reject_fact(rejected.fact_id)

    a_2021 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2021", source_id=source_a)
    source_b = s.add_source("sources/b.txt")
    b_2021 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2021", source_id=source_b)

    applied = apply_auto_accept_recommendations(s)

    applied_ids = {r.fact_id for r in applied}
    assert {a_2021.fact_id, b_2021.fact_id} <= applied_ids
    assert s.get_fact(a_2021.fact_id)["status"] == "accepted"
    assert s.get_fact(b_2021.fact_id)["status"] == "accepted"
    assert s.get_fact(rejected.fact_id)["status"] == "superseded"


def test_human_accept_of_a_relabeled_candidate_still_witnesses(tmp_path):
    # Human-override preserved -- this is what proves part (B)'s is_review_eligible
    # scoping is correct and does not overreach. After the relabeled candidate is
    # resurrected, a human explicitly accepts it (-> confirmed, the engine tier).
    # That deliberate decision must not be discounted: even though the confirmed
    # fact is itself reject-shadowed (same source as the rejection), it must still
    # count as a witness. A third source then corroborates, and the third fact is
    # eligible on the strength of {confirmed A, fresh C}. Were the exclusion not
    # scoped to review-eligible, the confirmed A witness would drop and C would starve.
    _write_rename_policy(tmp_path)
    s = _store(tmp_path)

    source_a = s.add_source("sources/a.txt")
    f1 = _reconciled_fact(s, subject="Corp", relation="sales", obj="2020", source_id=source_a)
    s.reject_fact(f1.fact_id)
    f2 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=source_a)
    s.accept_fact(f2.fact_id)
    assert s.get_fact(f2.fact_id)["status"] == "confirmed"

    source_c = s.add_source("sources/c.txt")
    f3 = _reconciled_fact(s, subject="Corp", relation="revenue", obj="2020", source_id=source_c)

    rec = accept_recommendation(s, f3.fact_id)

    assert rec is not None
    assert rec.eligible is True
    # The confirmed (human-decided) A witness still counts alongside fresh C.
    assert set(rec.support_sources) == {"sources/a.txt", "sources/c.txt"}


def test_one_sources_rejection_does_not_shadow_two_fresh_sources(tmp_path):
    # INTENTIONAL SCOPE of #343, not a gap: one source's rejection is NOT a global
    # veto. Whether enough genuinely independent later corroboration should override
    # an earlier rejection is a policy question #343 explicitly leaves open, so the
    # shadow is source-scoped -- two never-rejected sources still promote the same
    # value the way they always could. A source-independent shadow would silently
    # answer that open question by vetoing the value forever.
    _write_rename_policy(tmp_path)
    s = _store(tmp_path)

    source_a = s.add_source("sources/a.txt")
    f1 = _reconciled_fact(s, subject="Corp", relation="sales", obj="2020", source_id=source_a)
    s.reject_fact(f1.fact_id)

    fresh = []
    for path in ("sources/b.txt", "sources/c.txt"):
        src = s.add_source(path)
        fresh.append(
            _reconciled_fact(
                s, subject="Corp", relation="revenue", obj="2020", source_id=src
            ).fact_id
        )

    applied = apply_auto_accept_recommendations(s)

    applied_ids = {r.fact_id for r in applied}
    assert set(fresh) <= applied_ids
    for fid in fresh:
        assert s.get_fact(fid)["status"] == "accepted"
    assert s.get_fact(f1.fact_id)["status"] == "superseded"
