# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.acceptance import (
    accept_recommendation,
    apply_auto_accept_recommendations,
    RULE_NAME,
)
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
