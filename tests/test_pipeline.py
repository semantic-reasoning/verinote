# SPDX-License-Identifier: MPL-2.0
import json

import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline import (
    create_chunked_extraction_job,
    ExtractionJobBusyError,
    extract_source,
    process_extraction_job,
    sync_sources,
)
from verinote.pipeline.extract import _canonical_fact
from verinote.pipeline.query import query_path
from verinote.pipeline.verify import verify
from verinote.policy_defaults import RELATION_ALIASES_RELPATH
from verinote.store import Store
from verinote.engine.terms import Atom, Compound, StringLit


_BORN_IN_QUERY = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(O) :- relation("Ada", "born_in", O).\n'
)


def _write_born_in_query(root):
    path = query_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BORN_IN_QUERY, encoding="utf-8")


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _write_relation_aliases(root, body: str) -> None:
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


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
    # Stored with its raw label now that write-time aliasing is gone (#252);
    # ``제공기능`` canonicalizes to ``provides`` only at read time.
    assert fact["relation"] == "제공기능"
    assert fact["object"] == "샘플기능"


def test_extract_source_drops_reversed_role_designation(tmp_path, fake_client):
    # ``org 역할 person`` is the reversed shape of ``person 역할 role``: it names a
    # person as the OBJECT of a role designation even though that person holds a
    # role themselves in the same batch. Drop it; keep the correct direction.
    # The filter still canonicalizes 역할 -> ``role`` to make the drop decision,
    # but the kept fact is now stored with its raw ``역할`` label (#252).
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
    assert ("샘플인물", "역할", "샘플직책") in facts
    assert ("샘플조직", "역할", "샘플인물") not in facts


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


def test_extract_source_stores_raw_alias_labels(tmp_path, fake_client):
    # #252: an aliased relation is stored with the label the source used, never
    # overwritten by its canonical form — canonicalization is a read-time concern.
    # `설립` and `founded` both alias to `established_on`, but storage keeps the raw
    # words. `founded` (an ASCII alias KEY absent from the Korean source) also
    # exercises the ascii-legitimacy filter: it must canonicalize its own decision
    # so a real aliased fact is kept, not dropped as an unbacked hallucination.
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact("샘플회사", "설립", "2020", 0.9),
            ExtractedFact("샘플회사", "founded", "2021", 0.9),
        ]
    )

    n = extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="샘플회사는 2020년에 설립되었고 2021년 기록도 있다",
        run_id=run_id,
    )

    assert n == 2
    stored = {(f["subject"], f["relation"], f["object"]) for f in s.facts()}
    assert stored == {
        ("샘플회사", "설립", "2020"),
        ("샘플회사", "founded", "2021"),
    }


def test_extract_source_keeps_aliased_ascii_relation_but_drops_hallucination(
    tmp_path, fake_client
):
    # The ascii-legitimacy filter must separate an aliased key it should keep from
    # a genuine hallucination it should drop (#252): `founded` aliases to a
    # policy-backed relation and is kept raw, while `invented_by` is not an alias
    # and is absent from the source, so it is still dropped. Proves the filter was
    # made alias-aware, not simply disabled.
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [
            ExtractedFact("샘플회사", "founded", "2020", 0.9),
            ExtractedFact("샘플회사", "invented_by", "누군가", 0.9),
        ]
    )

    n = extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="샘플회사에 관한 국문 기록이다",
        run_id=run_id,
    )

    assert n == 1
    assert [(f["subject"], f["relation"], f["object"]) for f in s.facts()] == [
        ("샘플회사", "founded", "2020")
    ]


def test_extract_source_keeps_aliased_hanja_relation(tmp_path, fake_client):
    # A user-defined CJK/Hanja alias key (`設立` -> `established_on`) against a
    # Hangul-only source must be kept and stored raw (#252). Before the fix the
    # han-translation filter saw the raw `設立` — a CJK run absent from a Hangul
    # source — and dropped it as a hallucinated translation; it must canonicalize
    # the relation for that check so a legitimate aliased fact survives.
    s = _store(tmp_path)
    _write_relation_aliases(tmp_path, "- `設立` -> `established_on`\n")
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client([ExtractedFact("샘플회사", "設立", "2020", 0.9)])

    n = extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="샘플회사에 관한 국문 기록이다",
        run_id=run_id,
    )

    assert n == 1
    fact = s.facts()[0]
    assert (fact["subject"], fact["relation"], fact["object"]) == (
        "샘플회사",
        "設立",
        "2020",
    )


def test_canonical_fact_keeps_raw_relation_and_shape_normalization():
    # #252 storage boundary: `_canonical_fact` no longer applies KB aliases, so an
    # aliased relation is returned unchanged (raw). The two shape normalizations it
    # is still responsible for stay intact: `_is_bad_spo_shape` drops a malformed
    # fragment, and the hardcoded `값`/`value` family collapses to `value`.
    aliased = ExtractedFact("샘플회사", "설립", "2020", 0.9)
    assert _canonical_fact(aliased) is aliased

    value_key = ExtractedFact("문서번호", "값", "A-001", 0.9)
    assert _canonical_fact(value_key).relation == "value"

    malformed = ExtractedFact("샘플", "판단 여부", "예", 0.9)
    assert _canonical_fact(malformed) is None


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


def test_process_extraction_job_backs_off_when_already_claimed(tmp_path):
    """A job another worker already owns raises `ExtractionJobBusyError` and is left
    entirely alone — its in-flight chunk is not reset back to the queue (#240)."""
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=2
    )
    chunk_ids = s.add_source_chunks(
        job_id=job_id, source_id=sid, chunks=["alpha", "beta"]
    )
    # The existing owner: the job is `running` with a chunk in flight.
    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_ids[0])
    runs_before = s._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    client = _ChunkAwareClient()

    with pytest.raises(ExtractionJobBusyError):
        process_extraction_job(s, client, job_id=job_id)

    assert s.source_chunks(job_id)[0]["status"] == "running"  # owner's chunk untouched
    assert client.calls == 0  # the LLM was never reached
    assert len(s.facts()) == 0  # no candidate facts written
    runs_after = s._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    assert runs_after == runs_before  # no new run row opened


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


def _extract_one_chunk_at_artifact(s, client, *, source_id, artifact_id, chunk):
    """Run a fresh one-chunk chunked job over `chunk` at `artifact_id`."""
    job_id = s.create_extraction_job(
        source_id=source_id,
        artifact_id=artifact_id,
        provider="fake",
        model="m",
        total_chunks=1,
    )
    s.add_source_chunks(job_id=job_id, source_id=source_id, chunks=[chunk])
    return process_extraction_job(s, client, job_id=job_id)


def test_process_extraction_job_reanchors_a_reobserved_fact_at_the_new_artifact(
    tmp_path, fake_client
):
    # #329 Part A: a clean re-extraction re-hits an existing confirmed fact.
    # reconcile_fact inserts nothing on the hit, so the only way the run leaves a
    # trace at the new artifact is note_fact_reobserved anchoring fresh evidence
    # there -- and it must do so without disturbing the human's confirm.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_one = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    fact = ExtractedFact("alpha", "seen_in", "source", 0.9)

    first = _extract_one_chunk_at_artifact(
        s, fake_client([fact]), source_id=sid, artifact_id=artifact_one, chunk="alpha"
    )
    assert first.candidates == 1
    [row] = s.facts()
    s.toggle_review(row["id"])  # a human confirms it -- the #329 trust tier
    assert s.get_fact(row["id"])["status"] == "confirmed"

    artifact_two = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    second = _extract_one_chunk_at_artifact(
        s, fake_client([fact]), source_id=sid, artifact_id=artifact_two, chunk="alpha"
    )

    assert second.candidates == 0  # a dedupe hit, nothing created
    assert len(s.facts()) == 1
    # Re-anchoring evidence never touches the fact's status.
    assert s.get_fact(row["id"])["status"] == "confirmed"
    evidence = s.fact_evidence(row["id"])
    assert {e["artifact_id"] for e in evidence} == {artifact_one, artifact_two}


def test_process_extraction_job_does_not_reanchor_a_superseded_fact(
    tmp_path, fake_client
):
    # A superseded (human-rejected) fact is never re-anchored: reconcile_fact
    # records its suppression event and the hit branch leaves evidence untouched,
    # so the new artifact gets no anchor (mirrors reextraction_suppressed --
    # event only, no evidence).
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    artifact_one = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    fact = ExtractedFact("alpha", "seen_in", "source", 0.9)

    _extract_one_chunk_at_artifact(
        s, fake_client([fact]), source_id=sid, artifact_id=artifact_one, chunk="alpha"
    )
    [row] = s.facts()
    s.reject_fact(row["id"])
    assert s.get_fact(row["id"])["status"] == "superseded"

    artifact_two = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    second = _extract_one_chunk_at_artifact(
        s, fake_client([fact]), source_id=sid, artifact_id=artifact_two, chunk="alpha"
    )

    assert second.candidates == 0
    evidence = s.fact_evidence(row["id"])
    assert {e["artifact_id"] for e in evidence} == {artifact_one}  # no anchor at v2
    events = [
        e
        for e in s.fact_events(row["id"])
        if e["event_type"] == "reextraction_suppressed"
    ]
    assert len(events) == 1


def test_extract_source_does_not_resurrect_a_rejected_fact(tmp_path, fake_client):
    # The headline #160 regression: a human rejects an extracted fact, then the
    # same source is synced again. The rejection is terminal, so the re-sync must
    # not bring the fact back as a fresh candidate.
    s = _store(tmp_path)
    facts = [ExtractedFact("A", "is_a", "B", 0.9, "n")]

    run_one = s.add_run(provider="fake", model="m")
    assert (
        extract_source(
            s,
            fake_client(facts),
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_one,
        )
        == 1
    )
    [fact] = s.facts()
    s.reject_fact(fact["id"])
    assert s.get_fact(fact["id"])["status"] == "superseded"

    run_two = s.add_run(provider="fake", model="m")
    assert (
        extract_source(
            s,
            fake_client(facts),
            source_path="sources/x.txt",
            source_text="...",
            run_id=run_two,
        )
        == 0
    )

    rows = s.facts()
    assert len(rows) == 1
    assert rows[0]["id"] == fact["id"]
    assert rows[0]["status"] == "superseded"


def test_extract_source_reruns_add_no_duplicate_row_or_evidence(tmp_path, fake_client):
    s = _store(tmp_path)
    facts = [ExtractedFact("A", "is_a", "B", 0.9, "n")]

    assert (
        extract_source(
            s, fake_client(facts), source_path="sources/x.txt", source_text="..."
        )
        == 1
    )
    evidence_before = s._conn.execute("SELECT COUNT(*) FROM fact_evidence").fetchone()[0]

    assert (
        extract_source(
            s, fake_client(facts), source_path="sources/x.txt", source_text="..."
        )
        == 0
    )

    assert len(s.facts()) == 1
    # A dedupe hit attaches no fresh evidence -- pins the no-evidence-on-hit rule.
    assert (
        s._conn.execute("SELECT COUNT(*) FROM fact_evidence").fetchone()[0]
        == evidence_before
    )


def test_extract_source_rerun_still_inserts_a_genuinely_new_fact(tmp_path, fake_client):
    s = _store(tmp_path)
    first = [ExtractedFact("A", "is_a", "B", 0.9, "n")]
    assert (
        extract_source(
            s, fake_client(first), source_path="sources/x.txt", source_text="..."
        )
        == 1
    )

    second = [
        ExtractedFact("A", "is_a", "B", 0.9, "n"),
        ExtractedFact("C", "is_a", "D", 0.8),
    ]
    assert (
        extract_source(
            s, fake_client(second), source_path="sources/x.txt", source_text="..."
        )
        == 1
    )

    assert {(f["subject"], f["object"]) for f in s.facts()} == {("A", "B"), ("C", "D")}


def test_sync_records_suppression_event_when_reextracting_rejected_fact(
    tmp_path, fake_client
):
    # End-to-end #160: sync, reject, sync again. The re-sync creates no candidate
    # and records a reextraction_suppressed event on the original rejected fact.
    s = _store(tmp_path)
    facts = [ExtractedFact("A", "is_a", "B", 0.9, "n")]

    sync_sources(
        s,
        fake_client(facts),
        [("sources/x.txt", "...")],
        provider="fake",
        model="m",
    )
    [fact] = s.facts()
    s.reject_fact(fact["id"])

    result = sync_sources(
        s,
        fake_client(facts),
        [("sources/x.txt", "...")],
        provider="fake",
        model="m",
    )

    assert result.total == 0
    assert len(s.facts()) == 1
    events = [
        event
        for event in s.fact_events(fact["id"])
        if event["event_type"] == "reextraction_suppressed"
    ]
    assert len(events) == 1
    assert json.loads(events[0]["after_json"])["run_id"] == result.run_id


# --- #329 Part B: the staleness sweep, end to end ----------------------------


def test_stale_confirmed_fact_is_demoted_after_the_source_text_changes(
    tmp_path, fake_client
):
    # THE primary repro: confirm "Ada born_in London" -> edit the source to Paris
    # -> a clean re-extraction -> the sweep returns London to review (stale=1) and
    # the engine stops verifying it, while Paris sits as a fresh candidate.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    art_london = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    _extract_one_chunk_at_artifact(
        s,
        fake_client([ExtractedFact("Ada", "born_in", "London", 0.9)]),
        source_id=sid,
        artifact_id=art_london,
        chunk="Ada was born in London.",
    )
    [fact] = s.facts()
    s.toggle_review(fact["id"])  # a human confirms it
    assert s.get_fact(fact["id"])["status"] == "confirmed"
    _write_born_in_query(tmp_path)
    assert verify(s).answers == ["q1: London"]

    art_paris = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    outcome = _extract_one_chunk_at_artifact(
        s,
        fake_client([ExtractedFact("Ada", "born_in", "Paris", 0.9)]),
        source_id=sid,
        artifact_id=art_paris,
        chunk="Ada was born in Paris.",
    )

    demoted = s.surface_stale_engine_facts(outcome.job_id)

    assert [d["object"] for d in demoted] == ["London"]
    london = next(f for f in s.facts() if f["object"] == "London")
    paris = next(f for f in s.facts() if f["object"] == "Paris")
    assert london["status"] == "needs_review" and london["stale"] == 1
    assert paris["status"] == "candidate"
    # The engine no longer returns London, and Paris is not yet engine-tier.
    assert verify(s).answers == []


def test_unchanged_source_resync_demotes_nothing(tmp_path, fake_client):
    # The load-bearing anti-thrash regression: re-syncing an UNCHANGED source (the
    # ordinary case) re-observes its confirmed fact at the same content-addressed
    # artifact, so the sweep finds current evidence and demotes nothing. Without
    # this the whole design would oscillate under normal operation.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    art = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a.txt", checksum="v1"
    )
    london = ExtractedFact("Ada", "born_in", "London", 0.9)
    _extract_one_chunk_at_artifact(
        s, fake_client([london]), source_id=sid, artifact_id=art, chunk="London body."
    )
    [fact] = s.facts()
    s.toggle_review(fact["id"])
    assert s.get_fact(fact["id"])["status"] == "confirmed"

    outcome = _extract_one_chunk_at_artifact(
        s, fake_client([london]), source_id=sid, artifact_id=art, chunk="London body."
    )
    demoted = s.surface_stale_engine_facts(outcome.job_id)

    assert demoted == []
    assert s.get_fact(fact["id"])["status"] == "confirmed"
    assert s.get_fact(fact["id"])["stale"] == 0


def test_a_run_with_a_failed_chunk_demotes_nothing(tmp_path, fake_client):
    # Criterion 2 from the issue: a run with ANY failed chunk finishes 'failed' and
    # is self-gated out of staleness judgment entirely -- a partially-failed run
    # must never sweep away a confirmed fact just because it did not re-see it.
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    art_old = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    _extract_one_chunk_at_artifact(
        s,
        fake_client([ExtractedFact("alpha", "seen_in", "source", 0.9)]),
        source_id=sid,
        artifact_id=art_old,
        chunk="alpha",
    )
    [fact] = s.facts()
    s.toggle_review(fact["id"])
    assert s.get_fact(fact["id"])["status"] == "confirmed"

    art_new = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v2.txt", checksum="v2"
    )
    job_id = s.create_extraction_job(
        source_id=sid, artifact_id=art_new, provider="fake", model="m", total_chunks=1
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["bad"])
    result = process_extraction_job(s, _ChunkAwareClient(), job_id=job_id)
    assert result.failed_chunks == 1
    assert s.get_extraction_job(job_id)["status"] == "failed"

    assert s.surface_stale_engine_facts(job_id) == []
    assert s.get_fact(fact["id"])["status"] == "confirmed"
    assert s.get_fact(fact["id"])["stale"] == 0


def test_multi_source_demotes_only_the_edited_source_and_a_witness_still_answers(
    tmp_path, fake_client
):
    # Same triple confirmed from source A and B; A's text is edited away. Only A's
    # citation is demoted (facts are source-scoped), B's is untouched, and the
    # engine still answers because B remains a live witness.
    s = _store(tmp_path)
    london = ExtractedFact("Ada", "born_in", "London", 0.9)
    src_a = s.add_source("sources/a.txt")
    a_old = s.add_source_artifact(
        source_id=src_a, kind="original_text", path="sources/a-v1.txt", checksum="a1"
    )
    _extract_one_chunk_at_artifact(
        s, fake_client([london]), source_id=src_a, artifact_id=a_old, chunk="A: London."
    )
    a_fact = s.facts()[0]
    s.toggle_review(a_fact["id"])

    src_b = s.add_source("sources/b.txt")
    b_art = s.add_source_artifact(
        source_id=src_b, kind="original_text", path="sources/b.txt", checksum="b1"
    )
    _extract_one_chunk_at_artifact(
        s, fake_client([london]), source_id=src_b, artifact_id=b_art, chunk="B: London."
    )
    b_fact = next(f for f in s.facts() if f["source_id"] == src_b)
    s.toggle_review(b_fact["id"])

    _write_born_in_query(tmp_path)
    assert verify(s).answers == ["q1: London"]

    a_new = s.add_source_artifact(
        source_id=src_a, kind="original_text", path="sources/a-v2.txt", checksum="a2"
    )
    outcome = _extract_one_chunk_at_artifact(
        s,
        fake_client([ExtractedFact("Ada", "born_in", "Paris", 0.9)]),
        source_id=src_a,
        artifact_id=a_new,
        chunk="A: Paris.",
    )

    demoted = s.surface_stale_engine_facts(outcome.job_id)

    assert [d["id"] for d in demoted] == [a_fact["id"]]
    assert s.get_fact(a_fact["id"])["status"] == "needs_review"
    assert s.get_fact(b_fact["id"])["status"] == "confirmed"  # the witness is untouched
    # The engine still answers London -- through B, the live witness.
    assert verify(s).answers == ["q1: London"]
