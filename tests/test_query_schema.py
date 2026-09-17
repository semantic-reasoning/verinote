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


def test_snapshot_bounds_join_evidence_and_marks_truncation(tmp_path):
    s = _store(tmp_path)
    s.add_fact("A", "first", "M1", status="confirmed")
    s.add_fact("M1", "second", "Answer", status="confirmed")

    snapshot = build_query_schema_snapshot(
        s, bounds=QuerySchemaBounds(max_join_facts=1)
    )

    assert snapshot.join_facts == ()
    bounded = build_query_schema_snapshot(
        s, bounds=QuerySchemaBounds(max_join_facts=1), include_join_facts=True
    )
    assert [fact.fact_id for fact in bounded.join_facts] == [1]
    assert bounded.join_facts_truncated is True


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


def test_typed_declaration_attaches_through_a_sibling_raw_label(tmp_path):
    """#597 AC-1, both directions, end-to-end through
    `build_query_schema_snapshot` (AC-5): asserting the lookup function alone
    would miss partial resolution the same way the code did.

    The declaration is written under `설립일`, which the packaged table routes
    to `established_on` -- as does every sibling raw label. Two fact rows can
    sit under that canonical, and BOTH were `typed=None` on the pre-fix tree:

    - the reported case: the fact under the SIBLING raw label `창립일` -- the
      old four-key probe tried the row's own labels only, never the dict key
      the declaration was stored under;
    - the mirror: the fact under the CANONICAL label itself -- the probe
      tried `established_on` and never `설립일`, so it missed the declaration
      even when the row's display was the canonical.

    In both, the entry is present now and carries the DECLARED label, not the
    row's own: the entry is the user's declaration, not a description of the
    fact.
    """
    def _decl_store(root):
        s = _store(root)
        policy = root / "policy"
        policy.mkdir()
        (policy / "typed-relations.md").write_text(
            "- `설립일` : date as founded\n",
            encoding="utf-8",
        )
        return s

    # Arm 1: fact under the sibling raw label (the issue's repro).
    s = _decl_store(tmp_path / "sibling")
    s.add_fact("Company A", "창립일", "2005-06-07", status="confirmed")
    snapshot = build_query_schema_snapshot(s)
    relation = snapshot.relations[0]
    assert (relation.relation.display, relation.canonical_relation) == (
        "창립일",
        "established_on",
    )
    assert relation.typed is not None
    assert (relation.typed.relation, relation.typed.type, relation.typed.alias) == (
        "설립일",
        "date",
        "founded",
    )
    # The global typed list already carried the declaration before the fix;
    # the per-row entry is what was missing, and it is value-identical to the
    # one there, so a planner filtering by canonical sees exactly one spec.
    assert [(e.relation, e.type, e.alias) for e in snapshot.typed_relations] == [
        ("설립일", "date", "founded")
    ]

    # Arm 2 (mirror): fact under the canonical label, declaration under a
    # sibling raw label.
    m = _decl_store(tmp_path / "mirror")
    m.add_fact("Company B", "established_on", "2001-01-01", status="confirmed")
    mirror = build_query_schema_snapshot(m).relations[0]
    assert (mirror.relation.display, mirror.canonical_relation) == (
        "established_on",
        "established_on",
    )
    assert mirror.typed is not None
    assert (mirror.typed.relation, mirror.typed.type, mirror.typed.alias) == (
        "설립일",
        "date",
        "founded",
    )


def test_same_label_and_canonical_declaration_still_attach_the_written_label(tmp_path):
    """#597 AC-3. The two cases that already worked keep their typed entry,
    and the entry keeps the label the user WROTE -- byte-for-byte the
    invariant the pre-fix probe produced:

    - declaration and fact share the raw label   -> entry.label is that label;
    - declaration under the canonical itself      -> entry.label is the canonical.

    A fix that re-keyed the entry onto the canonical (or the fact's label)
    would pass the sibling-label test above while silently rewriting the label
    lists these rows already rendered.
    """
    # Arm 1: declaration and fact share the raw label.
    a = _store(tmp_path / "same")
    (tmp_path / "same" / "policy").mkdir()
    (tmp_path / "same" / "policy" / "typed-relations.md").write_text(
        "- `설립일` : date as founded\n", encoding="utf-8"
    )
    a.add_fact("Company A", "설립일", "2001-03-04", status="confirmed")
    same = build_query_schema_snapshot(a).relations[0]
    assert same.typed is not None
    assert (same.typed.relation, same.typed.type, same.typed.alias) == (
        "설립일",
        "date",
        "founded",
    )

    # Arm 2: declaration under the canonical label itself.
    b = _store(tmp_path / "canonical")
    (tmp_path / "canonical" / "policy").mkdir()
    (tmp_path / "canonical" / "policy" / "typed-relations.md").write_text(
        "- established_on : date as founded_on\n", encoding="utf-8"
    )
    b.add_fact("Company B", "창립일", "2005-06-07", status="confirmed")
    canonical = build_query_schema_snapshot(b).relations[0]
    assert canonical.typed is not None
    assert (canonical.typed.relation, canonical.typed.type, canonical.typed.alias) == (
        "established_on",
        "date",
        "founded_on",
    )


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
