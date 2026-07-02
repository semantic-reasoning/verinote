# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline import extract_source, sync_sources
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
