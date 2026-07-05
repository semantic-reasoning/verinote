# SPDX-License-Identifier: MPL-2.0

import unicodedata

from verinote.pipeline.query_intent import IntentTarget, QueryIntent, QueryIntentKind
from verinote.pipeline.query_planner import (
    QueryPlannerBounds,
    plan_query_candidates,
)
from verinote.pipeline.query_schema import (
    EntityRef,
    QuerySchemaSnapshot,
    RelationAliasEntry,
    RelationSchema,
    TermRef,
    TypedRelationEntry,
)


def _term(display: str, executable: str, kind: str = "StringLit") -> TermRef:
    return TermRef(
        display=display,
        executable=executable,
        kind=kind,
        key=f"{kind}:{executable}",
    )


def _entity(display: str, executable: str, kind: str = "StringLit") -> EntityRef:
    return EntityRef(
        display=display,
        executable=executable,
        kind=kind,
        key=f"{kind}:{executable}",
        fact_count=1,
    )


def _relation(
    display: str,
    executable: str,
    *,
    subjects: tuple[EntityRef, ...],
    objects: tuple[EntityRef, ...],
    canonical: str | None = None,
    aliases: tuple[RelationAliasEntry, ...] = (),
    typed: TypedRelationEntry | None = None,
    kind: str = "StringLit",
) -> RelationSchema:
    return RelationSchema(
        relation=_term(display, executable, kind),
        canonical_relation=canonical or display,
        aliases=aliases,
        typed=typed,
        fact_count=1,
        distinct_subject_count=len(subjects),
        distinct_object_count=len(objects),
        subjects=subjects,
        objects=objects,
        subjects_truncated=False,
        objects_truncated=False,
    )


def _snapshot(*relations: RelationSchema) -> QuerySchemaSnapshot:
    return QuerySchemaSnapshot(
        relations=relations,
        relations_truncated=False,
        relation_aliases=(),
        typed_relations=(),
        exact_entity_facts=(),
        exact_entity_facts_truncated=False,
        fact_count=len(relations),
    )


def test_lookup_object_uses_observed_relation_and_subject_side():
    sample_person = _entity("Sample Person", '"Sample Person"')
    snapshot = _snapshot(
        _relation(
            "역할",
            '"역할"',
            subjects=(sample_person,),
            objects=(_entity("Reviewer", '"Reviewer"'),),
        ),
        _relation(
            "역할",
            '"역할"',
            subjects=(_entity("Other Subject", '"Other Subject"'),),
            objects=(sample_person,),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation=IntentTarget("relation", "역할"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=12)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q12(value: symbol)\n'
        'answer_q12(O) :- relation("Sample Person", "역할", O).'
    ]
    assert plan.truncated is False


def test_lookup_subject_uses_object_side_directionality():
    snapshot = _snapshot(
        _relation(
            "역할",
            '"역할"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Reviewer", '"Reviewer"'),),
        ),
        _relation(
            "역할",
            '"역할"',
            subjects=(_entity("Reviewer", '"Reviewer"'),),
            objects=(_entity("Other Value", '"Other Value"'),),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_SUBJECT,
        relation=IntentTarget("relation", "역할"),
        object=IntentTarget("entity", "Reviewer"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=13)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q13(value: symbol)\n'
        'answer_q13(S) :- relation(S, "역할", "Reviewer").'
    ]


def test_lookup_relation_uses_intentional_variable_relation_only_for_relation_lookup():
    snapshot = _snapshot(
        _relation(
            "authored",
            '"authored"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Sample Document", '"Sample Document"'),),
        ),
        _relation(
            "reviewed",
            '"reviewed"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Sample Document", '"Sample Document"'),),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_RELATION,
        subject=IntentTarget("entity", "Sample Person"),
        object=IntentTarget("entity", "Sample Document"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=14)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q14(value: symbol)\n'
        'answer_q14(R) :- relation("Sample Person", R, "Sample Document").'
    ]


def test_source_language_relation_is_preserved_for_alias_backed_lookup():
    snapshot = _snapshot(
        _relation(
            "역할",
            '"역할"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Reviewer", '"Reviewer"'),),
            aliases=(RelationAliasEntry(alias="role", canonical="역할"),),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation_candidates=("role", "title"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=15)

    assert [candidate.relation_display for candidate in plan.candidates] == ["역할"]
    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q15(value: symbol)\n'
        'answer_q15(O) :- relation("Sample Person", "역할", O).'
    ]


def test_canonical_observed_relation_matches_raw_alias_without_rewrite():
    snapshot = _snapshot(
        _relation(
            "revenue",
            '"revenue"',
            subjects=(_entity("Synthetic Company", '"Synthetic Company"'),),
            objects=(_entity("100", '"100"'),),
            aliases=(RelationAliasEntry(alias="매출", canonical="revenue"),),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Synthetic Company"),
        relation=IntentTarget("relation", "매출"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=20)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q20(value: symbol)\n'
        'answer_q20(O) :- relation("Synthetic Company", "revenue", O).'
    ]


def test_unicode_nfd_observed_relation_matches_nfc_alias_policy():
    nfd_role = unicodedata.normalize("NFD", "역할")
    snapshot = _snapshot(
        _relation(
            nfd_role,
            f'"{nfd_role}"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Reviewer", '"Reviewer"'),),
            aliases=(RelationAliasEntry(alias="role", canonical="역할"),),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation=IntentTarget("relation", "role"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=21)

    assert [candidate.relation_executable for candidate in plan.candidates] == [
        f'"{nfd_role}"'
    ]


def test_typed_relation_metadata_can_match_without_inventing_relation_terms():
    snapshot = _snapshot(
        _relation(
            "매출액",
            '"매출액"',
            subjects=(_entity("Synthetic Company", '"Synthetic Company"'),),
            objects=(_entity("amount(5, \"억\")", 'amount(5, "억")', "Compound"),),
            canonical="revenue",
            typed=TypedRelationEntry(
                relation="revenue",
                type="amount",
                alias="revenue_scalar",
            ),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Synthetic Company"),
        relation=IntentTarget("relation", "revenue_scalar"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=16)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q16(value: symbol)\n'
        'answer_q16(O) :- relation("Synthetic Company", "매출액", O).'
    ]


def test_generic_typed_type_does_not_match_all_typed_amount_relations():
    snapshot = _snapshot(
        _relation(
            "매출액",
            '"매출액"',
            subjects=(_entity("Synthetic Company", '"Synthetic Company"'),),
            objects=(_entity("amount(5, \"억\")", 'amount(5, "억")', "Compound"),),
            typed=TypedRelationEntry(
                relation="매출액",
                type="amount",
                alias="revenue_scalar",
            ),
        ),
        _relation(
            "비용",
            '"비용"',
            subjects=(_entity("Synthetic Company", '"Synthetic Company"'),),
            objects=(_entity("amount(2, \"억\")", 'amount(2, "억")', "Compound"),),
            typed=TypedRelationEntry(
                relation="비용",
                type="amount",
                alias="cost_scalar",
            ),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Synthetic Company"),
        relation=IntentTarget("relation", "amount"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=22)

    assert plan.candidates == ()


def test_candidate_cap_truncates_alias_backed_matches_deterministically():
    snapshot = _snapshot(
        *(
            _relation(
                display,
                f'"{display}"',
                subjects=(_entity("Sample Person", '"Sample Person"'),),
                objects=(_entity(f"Value {index}", f'"Value {index}"'),),
                aliases=(RelationAliasEntry(alias=f"raw_{index}", canonical=display),),
            )
            for index, display in enumerate(("표시0", "표시1", "표시2"))
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation_candidates=("raw_0", "raw_1", "raw_2"),
    )

    plan = plan_query_candidates(
        intent,
        snapshot,
        qid=23,
        bounds=QueryPlannerBounds(max_candidates=2),
    )

    assert plan.truncated is True
    assert [candidate.relation_display for candidate in plan.candidates] == [
        "표시0",
        "표시1",
    ]


def test_role_title_lookup_uses_observed_schema_without_inventing_relations():
    snapshot = _snapshot(
        _relation(
            "직책",
            '"직책"',
            subjects=(_entity("Sample Person", '"Sample Person"'),),
            objects=(_entity("Editor", '"Editor"'),),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation_candidates=("역할", "직책", "직위", "has_role"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=17)

    assert [candidate.relation_display for candidate in plan.candidates] == ["직책"]
    assert "has_role" not in "\n".join(
        candidate.query_dl for candidate in plan.candidates
    )


def test_candidate_cap_truncates_deterministically():
    snapshot = _snapshot(
        *(
            _relation(
                relation,
                f'"{relation}"',
                subjects=(_entity("Sample Person", '"Sample Person"'),),
                objects=(_entity(f"Value {relation}", f'"Value {relation}"'),),
            )
            for relation in ("r0", "r1", "r2")
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Person"),
        relation_candidates=("r0", "r1", "r2"),
    )

    plan = plan_query_candidates(
        intent,
        snapshot,
        qid=18,
        bounds=QueryPlannerBounds(max_candidates=2),
    )

    assert plan.truncated is True
    assert [candidate.relation_display for candidate in plan.candidates] == ["r0", "r1"]


def test_structural_endpoint_terms_render_with_executable_identity():
    snapshot = _snapshot(
        _relation(
            "has_role",
            "has_role",
            kind="Atom",
            subjects=(_entity('person("Ada")', 'person("Ada")', "Compound"),),
            objects=(
                _entity(
                    'role(person("Ada"), "PI")',
                    'role(person("Ada"), "PI")',
                    "Compound",
                ),
            ),
        )
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", 'person("Ada")'),
        relation=IntentTarget("relation", "has_role"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=19)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q19(value: symbol)\n'
        'answer_q19(O) :- relation(person("Ada"), has_role, O).'
    ]
