# SPDX-License-Identifier: MPL-2.0
import unicodedata

import pytest

from verinote.engine.terms import Atom, Compound, StringLit
from verinote.pipeline.corroboration import CorroborationPolicyError
from verinote.pipeline.query_schema import (
    QuerySchemaBounds,
    build_query_schema_snapshot,
)
from verinote.store import Store
from verinote.store.fact_input import structural_term


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_snapshot_uses_engine_statuses_only_and_preserves_term_identity(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Beta", "mentions", "candidate", status="candidate")
    s.add_fact("Beta", "mentions", "review", status="needs_review")
    s.add_fact('person("Ada")', "mentions", "literal", status="confirmed")
    s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("mentions"),
        StringLit("compound"),
        status="accepted",
    )
    s.add_fact(Atom("ada"), Atom("mentions"), "atom", status="confirmed")

    snapshot = build_query_schema_snapshot(s)

    assert snapshot.fact_count == 3
    assert [
        (
            relation.relation.display,
            relation.relation.executable,
            relation.relation.kind,
            relation.fact_count,
        )
        for relation in snapshot.relations
    ] == [
        ("mentions", '"mentions"', "StringLit", 1),
        ("mentions", "mentions", "Atom", 2),
    ]

    string_relation = snapshot.relations[0]
    assert [
        (subject.display, subject.executable, subject.kind, subject.fact_count)
        for subject in string_relation.subjects
    ] == [
        ('person("Ada")', '"person(\\"Ada\\")"', "StringLit", 1),
    ]
    assert [obj.display for obj in string_relation.objects] == ["literal"]

    atom_relation = snapshot.relations[1]
    assert [
        (subject.display, subject.executable, subject.kind, subject.fact_count)
        for subject in atom_relation.subjects
    ] == [
        ("ada", "ada", "Atom", 1),
        ('person("Ada")', 'person("Ada")', "Compound", 1),
    ]
    assert [obj.display for obj in atom_relation.objects] == ['"compound"', "atom"]


def test_aliases_preserve_observed_nfd_relation_and_attach_canonical_metadata(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `게재연도` -> `published_year`\n"
        "- `발행년도` -> `published_year`\n",
        encoding="utf-8",
    )
    nfd_relation = unicodedata.normalize("NFD", "게재연도")
    s.add_fact("Paper A", nfd_relation, "2005", status="confirmed")
    s.add_fact("Paper B", "발행년도", "2007", status="confirmed")

    snapshot = build_query_schema_snapshot(s)

    assert [relation.relation.display for relation in snapshot.relations] == [
        nfd_relation,
        "발행년도",
    ]
    assert [relation.canonical_relation for relation in snapshot.relations] == [
        "published_year",
        "published_year",
    ]
    snapshot_aliases = [(a.alias, a.canonical) for a in snapshot.relation_aliases]
    assert ("게재연도", "published_year") in snapshot_aliases
    assert ("발행년도", "published_year") in snapshot_aliases
    assert [
        [(alias.alias, alias.canonical) for alias in relation.aliases]
        for relation in snapshot.relations
    ] == [
        [("게재연도", "published_year"), ("발행년도", "published_year")],
        [("게재연도", "published_year"), ("발행년도", "published_year")],
    ]


def test_typed_relation_metadata_attaches_through_canonical_alias_with_units(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `매출액` -> `revenue`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar (억원=100000000, 조원=1000000000000)\n",
        encoding="utf-8",
    )
    s.add_fact("Company A", "매출액", 'amount(5400,"억")', status="confirmed")

    snapshot = build_query_schema_snapshot(s)

    assert [(typed.relation, typed.type, typed.alias) for typed in snapshot.typed_relations] == [
        ("revenue", "amount", "revenue_scalar")
    ]
    relation = snapshot.relations[0]
    assert relation.typed is not None
    assert (relation.typed.relation, relation.typed.type, relation.typed.alias) == (
        "revenue",
        "amount",
        "revenue_scalar",
    )
    assert [(unit.unit, unit.scale) for unit in relation.typed.units] == [
        ("억원", 100000000),
        ("조원", 1000000000000),
    ]


def test_typed_policy_errors_are_not_swallowed(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "typed-relations.md").write_text(
        "- `출시일` : date as released_on (일=1)\n",
        encoding="utf-8",
    )

    with pytest.raises(CorroborationPolicyError, match="units are only valid"):
        build_query_schema_snapshot(s)


def test_directionality_counts_keep_subject_and_object_sides_separate(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Ada", "related_to", "Grace", status="confirmed")
    s.add_fact("Grace", "related_to", "Ada", status="confirmed")
    s.add_fact("Ada", "related_to", "Ada", status="confirmed")

    relation = build_query_schema_snapshot(s).relations[0]

    assert relation.fact_count == 3
    assert relation.distinct_subject_count == 2
    assert relation.distinct_object_count == 2
    assert [(ref.display, ref.fact_count) for ref in relation.subjects] == [
        ("Ada", 2),
        ("Grace", 1),
    ]
    assert [(ref.display, ref.fact_count) for ref in relation.objects] == [
        ("Ada", 2),
        ("Grace", 1),
    ]


def test_bounds_apply_to_relations_entities_and_exact_facts_deterministically(tmp_path):
    s = _store(tmp_path)
    for idx in range(3):
        relation = f"r{idx}"
        s.add_fact(f"S{idx}", relation, f"O{idx}", status="confirmed")
    for idx in range(3):
        s.add_fact(f"Entity {idx}", "wide", f"Value {idx}", status="confirmed")
    s.add_fact("Entity 3", "wide", "Needle", status="confirmed")

    snapshot = build_query_schema_snapshot(
        s,
        exact_entities=("Needle",),
        bounds=QuerySchemaBounds(
            max_relations=2,
            max_entities_per_side=2,
            max_exact_entity_facts=0,
        ),
    )

    assert [relation.relation.display for relation in snapshot.relations] == ["r0", "r1"]
    assert snapshot.relations_truncated is True
    wide = build_query_schema_snapshot(
        s,
        bounds=QuerySchemaBounds(max_relations=10, max_entities_per_side=2),
    ).relations[-1]
    assert wide.relation.display == "wide"
    assert [subject.display for subject in wide.subjects] == ["Entity 0", "Entity 1"]
    assert [obj.display for obj in wide.objects] == ["Needle", "Value 0"]
    assert wide.subjects_truncated is True
    assert wide.objects_truncated is True
    assert snapshot.exact_entity_facts == ()
    assert snapshot.exact_entity_facts_truncated is True


def test_exact_entity_matching_preserves_direction_and_structural_identity(tmp_path):
    s = _store(tmp_path)
    s.add_fact('person("Ada")', "knows", "Grace", status="confirmed")
    s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("knows"),
        Compound("person", (StringLit("Ada"),)),
        status="confirmed",
    )
    s.add_fact("Grace", "knows", structural_term('person("Ada")'), status="confirmed")

    snapshot = build_query_schema_snapshot(
        s,
        exact_entities=('"person(\\"Ada\\")"', 'person("Ada")'),
        bounds=QuerySchemaBounds(max_exact_entity_facts=10),
    )

    assert [
        (
            fact.subject.display,
            fact.subject.kind,
            fact.object.display,
            fact.object.kind,
            fact.matched_entity,
            fact.matched_side,
        )
        for fact in snapshot.exact_entity_facts
    ] == [
        ("Grace", "StringLit", 'person("Ada")', "Compound", 'person("Ada")', "object"),
        ('person("Ada")', "StringLit", "Grace", "StringLit", '"person(\\"Ada\\")"', "subject"),
        ('person("Ada")', "Compound", 'person("Ada")', "Compound", 'person("Ada")', "both"),
    ]


def test_exact_entity_matching_normalizes_unicode_and_preserves_display_label(tmp_path):
    s = _store(tmp_path)
    nfd_entity = unicodedata.normalize("NFD", "Café Entity")
    nfc_entity = unicodedata.normalize("NFC", "Café Entity")
    s.add_fact(nfd_entity, "mentions", "Synthetic Object", status="confirmed")
    s.add_fact("Synthetic Subject", "mentions", nfd_entity, status="confirmed")

    snapshot = build_query_schema_snapshot(
        s,
        exact_entities=(nfc_entity,),
        bounds=QuerySchemaBounds(max_exact_entity_facts=10),
    )

    assert [
        (
            fact.subject.display,
            fact.subject.kind,
            fact.object.display,
            fact.object.kind,
            fact.matched_entity,
            fact.matched_side,
        )
        for fact in snapshot.exact_entity_facts
    ] == [
        (nfd_entity, "StringLit", "Synthetic Object", "StringLit", nfc_entity, "subject"),
        ("Synthetic Subject", "StringLit", nfd_entity, "StringLit", nfc_entity, "object"),
    ]
