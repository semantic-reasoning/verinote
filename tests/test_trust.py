# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.trust import fact_trust_summary
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_fact_trust_summary_handles_fact_without_source_metadata(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="candidate",
        confidence=0.99,
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.display.subject == "Sample Company"
    assert summary.canonical_terms is not None
    assert summary.canonical_terms.object == '"Sample Service"'
    assert summary.source is None
    assert summary.run is None
    assert summary.job is None
    assert summary.evidence == ()
    assert summary.support.source_count == 0
    assert summary.conflict is None
    assert summary.review_eligible is True
    assert summary.engine_input is False
    assert summary.trust_labels == ("evidence_missing", "unsupported")


def test_fact_trust_summary_includes_source_run_job_evidence_and_audit(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("sources/sample-source.txt")
    artifact_id = s.add_source_artifact(
        source_id=source_id,
        kind="original_text",
        path="sources/sample-source.txt",
    )
    job_id = s.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="sample-model",
        total_chunks=1,
    )
    chunk_id = s.add_source_chunks(
        job_id=job_id,
        source_id=source_id,
        chunks=["Sample Company uses Sample Service."],
    )[0]
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)
    s.mark_chunk_done(chunk_id, candidates=1)
    run_id = s.add_run(provider="fake", model="sample-model", summary="sample run")
    fact_id = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="needs_review",
        source_id=source_id,
        run_id=run_id,
        job_id=job_id,
    )
    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        artifact_id=artifact_id,
        job_id=job_id,
        chunk_id=chunk_id,
        snippet="Sample Company uses Sample Service.",
    )
    s.toggle_review(fact_id)

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.source is not None
    assert summary.source.path == "sources/sample-source.txt"
    assert summary.run is not None
    assert summary.run.model == "sample-model"
    assert summary.job is not None
    assert summary.job.status == "done"
    assert len(summary.evidence) == 1
    assert summary.evidence[0].chunk_index == 0
    assert summary.evidence[0].chunk_status == "done"
    assert summary.evidence[0].snippet == "Sample Company uses Sample Service."
    assert [entry.action for entry in summary.audit] == ["toggled"]
    assert summary.trust_labels == ("source_backed", "single_source", "reviewed")


def test_fact_trust_summary_counts_distinct_engine_source_support(tmp_path):
    s = _store(tmp_path)
    source_a = s.add_source("sources/sample-a.txt")
    source_b = s.add_source("sources/sample-b.txt")
    first = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="confirmed",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="accepted",
        source_id=source_b,
    )
    s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="candidate",
        source_id=source_b,
    )

    summary = fact_trust_summary(s, first)

    assert summary is not None
    assert summary.support.source_count == 2
    assert summary.support.sources == (
        "sources/sample-a.txt",
        "sources/sample-b.txt",
    )
    assert "corroborated" in summary.trust_labels


def test_fact_trust_summary_includes_single_valued_conflict(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    source_a = s.add_source("sources/sample-a.txt")
    source_b = s.add_source("sources/sample-b.txt")
    fact_id = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="confirmed",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2025",
        status="accepted",
        source_id=source_b,
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.conflict is not None
    assert summary.conflict.relation == "published_year"
    assert [value.object for value in summary.conflict.values] == ["2024", "2025"]
    assert "conflicted" in summary.trust_labels


def test_fact_trust_summary_applies_relation_aliases(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `publication_year` -> `published_year`\n",
        encoding="utf-8",
    )
    fact_id = s.add_fact(
        "Sample Report",
        "publication_year",
        "2024",
        status="candidate",
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.canonical_relation == "published_year"


def test_fact_trust_summary_counts_alias_canonical_source_support(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `publication_year` -> `published_year`\n",
        encoding="utf-8",
    )
    source_a = s.add_source("sources/sample-a.txt")
    source_b = s.add_source("sources/sample-b.txt")
    fact_id = s.add_fact(
        "Sample Report",
        "publication_year",
        "2024",
        status="confirmed",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="accepted",
        source_id=source_b,
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.support.source_count == 2
    assert summary.support.sources == (
        "sources/sample-a.txt",
        "sources/sample-b.txt",
    )


def test_fact_trust_summary_normalizes_typed_scalar_value(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- rank : ordinal as rank_number\n",
        encoding="utf-8",
    )
    fact_id = s.add_fact(
        "Sample Service",
        "rank",
        "3rd",
        status="candidate",
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.typed_value is not None
    assert summary.typed_value.type == "ordinal"
    assert summary.typed_value.alias == "rank_number"
    assert summary.typed_value.normalized_value == 3


def test_fact_trust_summary_counts_typed_scalar_source_support(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- rank : ordinal as rank_number\n",
        encoding="utf-8",
    )
    source_a = s.add_source("sources/sample-a.txt")
    source_b = s.add_source("sources/sample-b.txt")
    fact_id = s.add_fact(
        "Sample Service",
        "rank",
        "3rd",
        status="confirmed",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Service",
        "rank",
        "ordinal(3)",
        status="accepted",
        source_id=source_b,
    )

    summary = fact_trust_summary(s, fact_id)

    assert summary is not None
    assert summary.support.source_count == 2
    assert "corroborated" in summary.trust_labels


def test_fact_trust_summary_labels_do_not_depend_on_confidence(tmp_path):
    s = _store(tmp_path)
    low = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="candidate",
        confidence=0.01,
    )
    high = s.add_fact(
        "Sample Company",
        "uses",
        "Sample Service",
        status="candidate",
        confidence=0.99,
    )

    low_summary = fact_trust_summary(s, low)
    high_summary = fact_trust_summary(s, high)

    assert low_summary is not None
    assert high_summary is not None
    assert low_summary.confidence == 0.01
    assert high_summary.confidence == 0.99
    assert low_summary.trust_labels == high_summary.trust_labels
