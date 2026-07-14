# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline import (
    create_chunked_extraction_job,
    extract_source,
    process_extraction_job,
    sync_sources,
)
from verinote.store import Store
from verinote.engine.terms import Atom, Compound, StringLit


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_extract_source_persists_candidates_with_linkage(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [ExtractedFact("A", "is_a", "B", 0.9, "n"), ExtractedFact("C", "rel", "D", 0.8)]
    )

    n = extract_source(
        s, client, source_path="sources/x.txt", source_text="...", run_id=run_id
    )

    assert n == 2
    facts = s.facts()
    assert [f["status"] for f in facts] == ["candidate", "candidate"]
    assert {f["run_id"] for f in facts} == {run_id}
    assert {f["source_path"] for f in facts} == {"sources/x.txt"}
    evidence = [s.fact_evidence(f["id"])[0] for f in facts]
    assert {e["evidence_kind"] for e in evidence} == {"chunk"}
    assert {e["locator"] for e in evidence} == {"source"}
    assert {e["snippet"] for e in evidence} == {"..."}


def test_extract_source_stores_term_syntax_as_plain_strings(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client([ExtractedFact('person("Ada")', "is_a", "person", 0.9)])

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert s.get_fact_terms(fact["id"]) == (
        StringLit('person("Ada")'),
        StringLit("is_a"),
        StringLit("person"),
    )


def test_extract_source_stores_explicit_structural_terms(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                'person("Ada")',
                "has_role",
                'role(person("Ada"), "PI")',
                0.9,
                subject_kind="term",
                relation_kind="term",
                object_kind="term",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert s.get_fact_terms(fact["id"]) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("has_role"),
        Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
    )


def test_extract_source_drops_typed_literals_in_subject_or_relation_slots(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    client = fake_client(
        [
            ExtractedFact(
                "샘플팀",
                "number(8)",
                "명",
                0.9,
                relation_kind="term",
            ),
            ExtractedFact(
                "number(8)",
                "인원",
                "명",
                0.9,
                subject_kind="term",
            ),
            ExtractedFact("샘플팀", "number(8)", "샘플값", 0.9),
            ExtractedFact("number(8)", "인원", "샘플값", 0.9),
            ExtractedFact("샘플팀", "인원", "명", 0.9),
            ExtractedFact(
                "샘플팀",
                "인원",
                "number(8)",
                0.9,
                object_kind="term",
                note="원문: 8명",
            ),
        ]
    )

    assert extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="샘플팀 인원 8명",
    ) == 1

    fact = s.facts()[0]
    assert fact["subject"] == "샘플팀"
    assert fact["relation"] == "인원"
    assert fact["object"] == "number(8)"
    assert fact["note"] == "원문: 8명"


def test_extract_source_stores_mixed_string_and_structural_terms(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "Example Corp",
                "legal_representative",
                'person("Ada")',
                1.0,
                subject_kind="string",
                relation_kind="string",
                object_kind="term",
                note="subject marked term but stored as string",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert s.get_fact_terms(fact["id"]) == (
        StringLit("Example Corp"),
        StringLit("legal_representative"),
        Compound("person", (StringLit("Ada"),)),
    )
    assert "stored as string" in fact["note"]


def test_extract_source_drops_string_to_term_normalization_bridge(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "Ada",
                "주체",
                'person("Ada")',
                1.0,
                object_kind="term",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []
    assert [source["path"] for source in s.sources()] == ["sources/x.txt"]


def test_extract_source_keeps_real_relation_to_structural_object(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "Example Corp",
                "legal_representative",
                'person("Ada")',
                1.0,
                object_kind="term",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert s.get_fact_terms(fact["id"]) == (
        StringLit("Example Corp"),
        StringLit("legal_representative"),
        Compound("person", (StringLit("Ada"),)),
    )


def test_extract_source_drops_chinese_translation_from_korean_source(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "示例企业",
                "拥有",
                "123个场所",
                0.9,
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플기업은 여러 사업장을 운영한다.",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_keeps_han_text_that_appears_in_korean_source(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "법인",
                "法定代表人",
                "Ada",
                0.9,
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="이 문서는 법인의 法定代表人을 명시한다.",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert s.get_fact_terms(fact["id"]) == (
        StringLit("법인"),
        StringLit("法定代表人"),
        StringLit("Ada"),
    )


def test_extract_source_drops_unbacked_ascii_relation_from_korean_source(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플파트너",
                "projected_revenue_2025",
                "123억원",
                0.9,
                note="123억원 2099 샘플매출",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플파트너\n샘플현황 123억원 2099 샘플매출",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_drops_metric_fact_when_subject_not_in_local_evidence(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플파트너",
                "샘플매출",
                "123억원",
                0.9,
                note="샘플현황 123억원 2099 샘플매출",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플파트너\n샘플현황 123억원 2099 샘플매출",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_ignores_hallucinated_note_for_metric_evidence(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플파트너",
                "샘플매출",
                "123억원",
                0.9,
                note="샘플파트너 2099 샘플매출 123억원",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플파트너\n샘플현황 123억원 2099 샘플매출",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_keeps_metric_fact_with_subject_in_local_evidence(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플기업",
                "샘플매출",
                "123억원",
                0.9,
                note="샘플기업 2099 샘플매출 123억원",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플기업 2099 샘플매출 123억원",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert fact["subject"] == "샘플기업"
    assert fact["relation"] == "샘플매출"
    assert fact["object"] == "123억원"


def test_extract_source_keeps_non_metric_korean_object_without_local_subject(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플서비스",
                "제공기능",
                "샘플기능",
                0.9,
                note="샘플 항목에는 색상·샘플기능이 포함된다",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플 항목에는 색상·샘플기능이 포함된다",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert fact["relation"] == "provides"
    assert fact["object"] == "샘플기능"


def test_extract_source_drops_reversed_role_designation(tmp_path, fake_client):
    # ``org 역할 person`` is the reversed shape of ``person 역할 role``: it names a
    # person as the OBJECT of a role designation even though that person holds a
    # role themselves in the same batch. Drop it; keep the correct direction.
    # (역할 canonicalizes to ``role`` under the default relation aliases.)
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact("샘플인물", "역할", "샘플직책", 0.9),
            ExtractedFact("샘플조직", "역할", "샘플인물", 0.9),
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플 문서 본문",
            run_id=run_id,
        )
        == 1
    )

    facts = {
        (str(f["subject"]), str(f["relation"]), str(f["object"])) for f in s.facts()
    }
    assert ("샘플인물", "role", "샘플직책") in facts
    assert ("샘플조직", "role", "샘플인물") not in facts


def test_extract_source_keeps_org_representative_role_fact(tmp_path, fake_client):
    # ``org 대표 person`` (the org's representative) has the same shape as a
    # reversed role designation, but the person here does NOT hold a role
    # elsewhere in the batch, so it is a legitimate fact and must survive — an
    # org-subject check alone would wrongly drop it. It keeps the label 대표: its
    # object is a person, not a title, so it is not the `role` relation and is
    # not aliased to it (#238).
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client([ExtractedFact("샘플조직주식회사", "대표", "샘플인물", 0.9)])

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플 문서 본문",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert (fact["subject"], fact["relation"], fact["object"]) == (
        "샘플조직주식회사",
        "대표",
        "샘플인물",
    )


def test_extract_source_canonicalizes_generic_value_relation(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "문서번호",
                "값",
                "A-001",
                0.9,
                note="문서번호 A-001",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="문서번호 A-001",
            run_id=run_id,
        )
        == 1
    )

    fact = s.facts()[0]
    assert fact["subject"] == "문서번호"
    assert fact["relation"] == "value"
    assert fact["object"] == "A-001"


def test_extract_source_allows_standard_value_relation_in_korean_source(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [ExtractedFact("문서번호", "value", "A-001", 0.9, note="문서번호 A-001")]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="문서번호 A-001",
            run_id=run_id,
        )
        == 1
    )

    assert s.facts()[0]["relation"] == "value"


def test_extract_source_allows_policy_backed_english_relation_in_korean_source(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플조직",
                "role",
                "샘플인물",
                0.9,
                note="샘플조직 샘플인물",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플조직 샘플인물",
            run_id=run_id,
        )
        == 1
    )

    assert s.facts()[0]["relation"] == "role"


def test_extract_source_drops_sentence_ending_object(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact(
                "샘플 입력",
                "처리 결과 여부",
                "입니다",
                0.9,
                note="샘플 입력을 처리하는 예시입니다",
            )
        ]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플 입력을 처리하는 예시입니다",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_drops_judgment_relation_shape(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [ExtractedFact("샘플 입력", "처리 결과 여부", "예", 0.9)]
    )

    assert (
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="샘플 입력을 처리하는 예시입니다",
            run_id=run_id,
        )
        == 0
    )

    assert s.facts() == []


def test_extract_source_normalizes_and_runs_focused_role_pass(tmp_path):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = _FocusedRoleClient()

    assert (
        extract_source(
            s,
            client,
            source_path="sources/a.pdf",
            source_text="발표· 성명대표| 샘플조직",
            run_id=run_id,
        )
        == 1
    )

    assert len(client.calls) == 2
    assert "성명 대표 (원문: 성명대표)" in client.calls[0][0]
    assert "Additional focused pass" in client.calls[1][1]
    assert "do not emit person-subject role facts" in client.calls[1][1]
    assert "canonical English relation labels" in client.calls[1][1]
    assert "same line, table row, bullet, or layout record" in client.calls[1][1]
    fact = s.facts()[0]
    assert fact["subject"] == "샘플조직"
    # 대표 keeps its own label: its object is a person, not a title, so it is not
    # the `role` relation and the defaults no longer alias it to one (#238).
    assert fact["relation"] == "대표"
    assert fact["object"] == "성명"
    assert fact["note"] == "원문: 성명대표"


def test_extract_source_rejects_invalid_structural_term_without_partial_facts(
    tmp_path, fake_client
):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact("A", "is_a", "B", 0.9),
            ExtractedFact(
                "person(Name)",
                "has_role",
                "PI",
                0.9,
                subject_kind="term",
            ),
        ]
    )

    with pytest.raises(LLMError, match="malformed extracted structural term"):
        extract_source(
            s,
            client,
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_id,
        )

    assert s.facts() == []
    assert s.sources() == []


def test_sync_sources_opens_run_and_summarises(tmp_path, fake_client):
    s = _store(tmp_path)
    client = fake_client([ExtractedFact("A", "is_a", "B", 0.9)])

    result = sync_sources(
        s,
        client,
        [("sources/a.txt", "ta"), ("sources/b.txt", "tb")],
        provider="fake",
        model="m",
    )

    assert result.total == 2
    assert result.per_source == [("sources/a.txt", 1), ("sources/b.txt", 1)]
    assert client.calls == 2

    run = s.get_run(result.run_id)
    assert run["provider"] == "fake" and run["model"] == "m"
    assert run["summary"] == "2 source(s), 2 candidate(s)"
    # every fact cites the one run that produced it
    assert {f["run_id"] for f in s.facts()} == {result.run_id}


def test_sync_sources_propagates_llm_error(tmp_path, fake_client):
    s = _store(tmp_path)
    client = fake_client(error=LLMError("no api key"))

    with pytest.raises(LLMError):
        sync_sources(s, client, [("sources/a.txt", "t")], provider="fake", model="m")


class _ChunkAwareClient:
    name = "chunk-aware"

    def __init__(self):
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls += 1
        if "bad" in source_text:
            raise LLMError("provider down")
        label = "alpha" if "alpha" in source_text else "beta"
        return [ExtractedFact(label, "seen_in", "source", 0.9)]


class _FocusedRoleClient:
    name = "focused-role"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls.append((source_text, schema_hint))
        if "Additional focused pass" not in schema_hint:
            return []
        return [
            ExtractedFact(
                "샘플조직",
                "대표",
                "성명",
                0.9,
                note="원문: 성명대표",
            )
        ]


class _FocusedRoleFailureClient:
    name = "focused-role-failure"

    def __init__(self):
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        self.calls += 1
        if "Additional focused pass" in schema_hint:
            raise LLMError("role pass failed")
        return [ExtractedFact("alpha", "출처", "source", 0.9)]


def test_create_chunked_extraction_job_persists_chunks(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    text = "alpha\n\n" + ("b" * 6100)

    job_id = create_chunked_extraction_job(
        s, source_id=sid, source_text=text, provider="fake", model="m"
    )

    job = s.get_extraction_job(job_id)
    chunks = s.source_chunks(job_id)
    assert job["source_id"] == sid
    assert job["total_chunks"] == len(chunks)
    assert len(chunks) >= 2
    assert chunks[0]["status"] == "pending"


def test_create_chunked_extraction_job_normalizes_role_text_for_analysis_chunks(
    tmp_path,
):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.pdf")

    job_id = create_chunked_extraction_job(
        s,
        source_id=sid,
        source_text="발표· 성명대표| 샘플조직",
        provider="fake",
        model="m",
        chunk_chars=1000,
        chunk_overlap_chars=0,
    )

    chunks = s.source_chunks(job_id)
    assert len(chunks) == 1
    assert "성명 대표 (원문: 성명대표)" in chunks[0]["text"]


def test_process_extraction_job_extracts_chunks_and_tracks_progress(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["alpha", "beta"])

    result = process_extraction_job(s, _ChunkAwareClient(), job_id=job_id)

    assert result.candidates == 2
    assert result.completed_chunks == 2
    assert result.failed_chunks == 0
    assert s.get_extraction_job(job_id)["status"] == "done"
    facts = s.facts()
    assert [f["subject"] for f in facts] == ["alpha", "beta"]
    assert {f["job_id"] for f in facts} == {job_id}
    evidence = [s.fact_evidence(f["id"])[0] for f in facts]
    assert [e["chunk_index"] for e in evidence] == [0, 1]
    assert {e["job_id"] for e in evidence} == {job_id}
    assert [e["snippet"] for e in evidence] == ["alpha", "beta"]


def test_process_extraction_job_runs_focused_role_pass_with_original_note(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.pdf")
    job_id = create_chunked_extraction_job(
        s,
        source_id=sid,
        source_text="발표· 성명대표| 샘플조직",
        provider="fake",
        model="m",
        chunk_chars=1000,
        chunk_overlap_chars=0,
    )
    client = _FocusedRoleClient()

    result = process_extraction_job(s, client, job_id=job_id)

    assert result.candidates == 1
    assert len(client.calls) == 2
    assert "성명 대표 (원문: 성명대표)" in client.calls[0][0]
    assert "Additional focused pass" in client.calls[1][1]
    assert "do not emit person-subject role facts" in client.calls[1][1]
    assert "canonical English relation labels" in client.calls[1][1]
    assert "same line, table row, bullet, or layout record" in client.calls[1][1]
    fact = s.facts()[0]
    assert fact["subject"] == "샘플조직"
    # 대표 keeps its own label: its object is a person, not a title, so it is not
    # the `role` relation and the defaults no longer alias it to one (#238).
    assert fact["relation"] == "대표"
    assert fact["object"] == "성명"
    assert fact["note"] == "원문: 성명대표"


def test_process_extraction_job_ignores_focused_role_pass_failure(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = create_chunked_extraction_job(
        s,
        source_id=sid,
        source_text="alpha 대표성명",
        provider="fake",
        model="m",
        chunk_chars=1000,
        chunk_overlap_chars=0,
    )

    result = process_extraction_job(s, _FocusedRoleFailureClient(), job_id=job_id)

    assert result.candidates == 1
    assert result.completed_chunks == 1
    assert result.failed_chunks == 0
    assert [f["subject"] for f in s.facts()] == ["alpha"]


def test_process_extraction_job_keeps_successful_chunks_when_one_fails(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["alpha", "bad"])

    result = process_extraction_job(s, _ChunkAwareClient(), job_id=job_id)

    assert result.candidates == 1
    assert result.completed_chunks == 1
    assert result.failed_chunks == 1
    assert s.get_extraction_job(job_id)["status"] == "failed"
    assert [chunk["status"] for chunk in s.source_chunks(job_id)] == ["done", "failed"]
    assert [f["subject"] for f in s.facts()] == ["alpha"]


def test_process_extraction_job_dedupes_chunk_facts_by_source(tmp_path, fake_client):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["alpha", "alpha again"])
    client = fake_client([ExtractedFact("alpha", "seen_in", "source", 0.9)])

    result = process_extraction_job(s, client, job_id=job_id)

    assert result.candidates == 1
    assert len(s.facts()) == 1
