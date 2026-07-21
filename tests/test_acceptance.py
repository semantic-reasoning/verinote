# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.acceptance import (
    accept_recommendation,
    apply_auto_accept_recommendations,
    RULE_NAME,
)
from verinote.pipeline.corroboration import store_single_valued_conflicts
from verinote.store import Store


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
    s.set_status(fact_id, "superseded", action="rejected")

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
