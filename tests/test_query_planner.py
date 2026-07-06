# SPDX-License-Identifier: MPL-2.0

import unicodedata

from verinote.pipeline.query_intent import IntentTarget, QueryIntent, QueryIntentKind
from verinote.pipeline.query_planner import (
    QueryCandidateDirection,
    QueryCandidateFamily,
    QueryPlannerBounds,
    plan_query_candidates,
)
from verinote.pipeline.query_schema import (
    EntityRef,
    QuerySchemaSnapshot,
    RelationAliasEntry,
    RelationSchema,
    SnapshotFact,
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


def _snapshot_with_exact(
    *relations: RelationSchema, exact_facts: tuple[SnapshotFact, ...]
) -> QuerySchemaSnapshot:
    return QuerySchemaSnapshot(
        relations=relations,
        relations_truncated=False,
        relation_aliases=(),
        typed_relations=(),
        exact_entity_facts=exact_facts,
        exact_entity_facts_truncated=False,
        fact_count=len(relations) + len(exact_facts),
    )


def _fact(
    subject: TermRef,
    relation: TermRef,
    obj: TermRef,
    *,
    matched_entity: str,
    matched_side: str,
) -> SnapshotFact:
    return SnapshotFact(
        fact_id=1,
        subject=subject,
        relation=relation,
        object=obj,
        status="confirmed",
        matched_entity=matched_entity,
        matched_side=matched_side,
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
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.DIRECT_OBJECT_LOOKUP
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.SUBJECT_TO_OBJECT
    ]
    assert plan.truncated is False


def test_entity_relation_discovery_generates_subject_side_candidates():
    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
        ),
        _snapshot(
            _relation(
                "provides",
                '"provides"',
                subjects=(_entity("Sample Entity", '"Sample Entity"'),),
                objects=(_entity("Sample Value", '"Sample Value"'),),
            )
        ),
        qid=9,
    )

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q9(value: symbol)\n'
        'answer_q9("provides") :- relation("Sample Entity", "provides", O).'
    ]
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.SUBJECT_RELATION_DISCOVERY
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.SUBJECT_TO_RELATION
    ]
    assert plan.reason is None


def test_entity_relation_discovery_generates_object_side_candidates_from_exact_facts():
    snapshot = _snapshot_with_exact(
        _relation(
            "mentions",
            '"mentions"',
            subjects=(_entity("Other Entity", '"Other Entity"'),),
            objects=(_entity("Other Value", '"Other Value"'),),
        ),
        exact_facts=(
            _fact(
                _term("Sample Source", '"Sample Source"'),
                _term("mentions", '"mentions"'),
                _term("Sample Entity", '"Sample Entity"'),
                matched_entity="Sample Entity",
                matched_side="object",
            ),
        ),
    )

    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
        ),
        snapshot,
        qid=10,
    )

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q10(value: symbol)\n'
        'answer_q10("mentions") :- relation(S, "mentions", "Sample Entity").'
    ]
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.OBJECT_RELATION_DISCOVERY
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.OBJECT_TO_RELATION
    ]


def test_entity_relation_discovery_keeps_direct_lookup_before_discovery():
    snapshot = _snapshot(
        _relation(
            "provides",
            '"provides"',
            subjects=(_entity("Sample Entity", '"Sample Entity"'),),
            objects=(_entity("Sample Value", '"Sample Value"'),),
        ),
        _relation(
            "owns",
            '"owns"',
            subjects=(_entity("Sample Entity", '"Sample Entity"'),),
            objects=(_entity("Other Value", '"Other Value"'),),
        ),
    )

    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
            relation=IntentTarget("relation", "provides"),
        ),
        snapshot,
        qid=11,
    )

    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.DIRECT_OBJECT_LOOKUP
    ]
    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q11(value: symbol)\n'
        'answer_q11(O) :- relation("Sample Entity", "provides", O).',
    ]


def test_entity_relation_discovery_uses_relation_hint_when_direct_lookup_missing():
    snapshot = _snapshot_with_exact(
        _relation(
            "provides",
            '"provides"',
            subjects=(_entity("Displayed Entity", '"Displayed Entity"'),),
            objects=(_entity("Displayed Value", '"Displayed Value"'),),
        ),
        exact_facts=(
            _fact(
                _term("Hidden Entity", '"Hidden Entity"'),
                _term("provides", '"provides"'),
                _term("Sample Value", '"Sample Value"'),
                matched_entity="Hidden Entity",
                matched_side="subject",
            ),
        ),
    )

    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Hidden Entity"),
            relation=IntentTarget("relation", "provides"),
        ),
        snapshot,
        qid=35,
    )

    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.EXACT_FACT_FALLBACK
    ]
    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q35(value: symbol)\n'
        'answer_q35(O) :- relation("Hidden Entity", "provides", O).',
    ]


def test_entity_relation_discovery_no_match_reason():
    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Missing Entity"),
        ),
        _snapshot(),
        qid=32,
    )

    assert plan.candidates == ()
    assert plan.reason == "no relation discovery candidates matched the schema"


def test_entity_relation_discovery_uses_exact_facts_when_schema_examples_omit_anchor():
    snapshot = _snapshot_with_exact(
        _relation(
            "supports",
            '"supports"',
            subjects=(_entity("Displayed Subject", '"Displayed Subject"'),),
            objects=(_entity("Displayed Value", '"Displayed Value"'),),
        ),
        exact_facts=(
            _fact(
                _term("Hidden Subject", '"Hidden Subject"'),
                _term("supports", '"supports"'),
                _term("Sample Value", '"Sample Value"'),
                matched_entity="Hidden Subject",
                matched_side="subject",
            ),
        ),
    )

    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Hidden Subject"),
        ),
        snapshot,
        qid=33,
    )

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q33(value: symbol)\n'
        'answer_q33("supports") :- relation("Hidden Subject", "supports", O).'
    ]


def test_entity_relation_discovery_candidate_cap_truncates_deterministically():
    snapshot = _snapshot(
        *(
            _relation(
                relation,
                f'"{relation}"',
                subjects=(_entity("Sample Entity", '"Sample Entity"'),),
                objects=(_entity(f"Value {relation}", f'"Value {relation}"'),),
            )
            for relation in ("alpha", "beta", "gamma")
        )
    )

    plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
        ),
        snapshot,
        qid=34,
        bounds=QueryPlannerBounds(max_candidates=2),
    )

    assert plan.truncated is True
    assert [candidate.relation_display for candidate in plan.candidates] == [
        "alpha",
        "beta",
    ]


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
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.DIRECT_SUBJECT_LOOKUP
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.OBJECT_TO_SUBJECT
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
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.DIRECT_RELATION_LOOKUP
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.SUBJECT_OBJECT_TO_RELATION
    ]


def test_lookup_relation_metadata_tracks_one_sided_direction():
    sample_person = _entity("Sample Person", '"Sample Person"')
    sample_document = _entity("Sample Document", '"Sample Document"')
    snapshot = _snapshot(
        _relation(
            "authored",
            '"authored"',
            subjects=(sample_person,),
            objects=(sample_document,),
        )
    )

    subject_plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_RELATION,
            subject=IntentTarget("entity", "Sample Person"),
        ),
        snapshot,
        qid=30,
    )
    object_plan = plan_query_candidates(
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_RELATION,
            object=IntentTarget("entity", "Sample Document"),
        ),
        snapshot,
        qid=31,
    )

    assert [candidate.direction for candidate in subject_plan.candidates] == [
        QueryCandidateDirection.SUBJECT_TO_RELATION
    ]
    assert [candidate.direction for candidate in object_plan.candidates] == [
        QueryCandidateDirection.OBJECT_TO_RELATION
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


def test_lookup_object_uses_exact_entity_facts_when_subject_examples_are_bounded():
    snapshot = _snapshot_with_exact(
        _relation(
            "role",
            '"role"',
            subjects=(_entity("Other Subject", '"Other Subject"'),),
            objects=(_entity("Other Value", '"Other Value"'),),
        ),
        exact_facts=(
            _fact(
                _term("Needle Subject", '"Needle Subject"'),
                _term("role", '"role"'),
                _term("Reviewer", '"Reviewer"'),
                matched_entity="Needle Subject",
                matched_side="subject",
            ),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Needle Subject"),
        relation=IntentTarget("relation", "role"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=24)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q24(value: symbol)\n'
        'answer_q24(O) :- relation("Needle Subject", "role", O).'
    ]
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.EXACT_FACT_FALLBACK
    ]
    assert [candidate.direction for candidate in plan.candidates] == [
        QueryCandidateDirection.SUBJECT_TO_OBJECT
    ]


def test_dedupe_keeps_pre_metadata_candidate_identity_for_same_query():
    sample_subject = _entity("Sample Subject", '"Sample Subject"')
    snapshot = _snapshot_with_exact(
        _relation(
            "role",
            '"role"',
            subjects=(sample_subject,),
            objects=(_entity("Reviewer", '"Reviewer"'),),
        ),
        exact_facts=(
            _fact(
                _term("Sample Subject", '"Sample Subject"'),
                _term("role", '"role"'),
                _term("Reviewer", '"Reviewer"'),
                matched_entity="Sample Subject",
                matched_side="subject",
            ),
        ),
    )
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "Sample Subject"),
        relation=IntentTarget("relation", "role"),
    )

    plan = plan_query_candidates(intent, snapshot, qid=25)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q25(value: symbol)\n'
        'answer_q25(O) :- relation("Sample Subject", "role", O).'
    ]
    assert [candidate.family for candidate in plan.candidates] == [
        QueryCandidateFamily.DIRECT_OBJECT_LOOKUP
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
