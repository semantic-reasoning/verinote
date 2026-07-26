# SPDX-License-Identifier: MPL-2.0

import unicodedata

from verinote.pipeline.query_intent import (
    ConjunctiveEndpoint,
    ConjunctiveHop,
    IntentTarget,
    QueryIntent,
    QueryIntentKind,
)
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
    UnitScale,
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


def _compare_intent(
    *,
    subject: str = "Synthetic Company",
    relation: str = "metric",
    operator: str = ">=",
    value_type: str = "number",
    value: str = "number(10)",
    relation_candidates: tuple[str, ...] = (),
) -> QueryIntent:
    return QueryIntent(
        kind=QueryIntentKind.COMPARE_TYPED_VALUE,
        subject=IntentTarget("entity", subject),
        relation=IntentTarget("relation", relation),
        relation_candidates=relation_candidates,
        operator=operator,
        value_type=value_type,
        value=value,
    )


def _typed_exact_snapshot(
    facts: tuple[SnapshotFact, ...],
    *,
    typed: tuple[TypedRelationEntry, ...],
    aliases: tuple[RelationAliasEntry, ...] = (),
    truncated: bool = False,
) -> QuerySchemaSnapshot:
    return QuerySchemaSnapshot(
        relations=(),
        relations_truncated=False,
        relation_aliases=aliases,
        typed_relations=typed,
        exact_entity_facts=facts,
        exact_entity_facts_truncated=truncated,
        fact_count=len(facts),
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


def test_conjunctive_lookup_uses_only_an_observed_join_path():
    ada = _term("Ada", '\"Ada\"')
    project = _term("Project", '\"Project\"')
    purpose = _term("Research", '\"Research\"')
    assigned = _term("assigned_to", '\"assigned_to\"')
    purpose_relation = _term("purpose", '\"purpose\"')
    snapshot = _snapshot()
    snapshot = QuerySchemaSnapshot(
        **{**snapshot.__dict__, "join_facts": (
            _fact(ada, assigned, project, matched_entity="Ada", matched_side="subject"),
            SnapshotFact(2, project, purpose_relation, purpose, "confirmed"),
            SnapshotFact(3, _term("Other", '\"Other\"'), purpose_relation, _term("Wrong", '\"Wrong\"'), "confirmed"),
        )}
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_LOOKUP,
        hops=(
            ConjunctiveHop(ConjunctiveEndpoint("entity", "Ada"), IntentTarget("relation", "assigned_to"), ConjunctiveEndpoint("var", "M")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "M"), IntentTarget("relation", "purpose"), ConjunctiveEndpoint("var", "A")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=17)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q17(value: symbol)\n'
        'answer_q17(A) :- relation("Ada", "assigned_to", M), relation(M, "purpose", A).'
    ]


def test_conjunctive_lookup_deduplicates_relation_paths_before_candidate_cap():
    assigned = _term("assigned_to", '\"assigned_to\"')
    purpose = _term("purpose", '\"purpose\"')
    facts = []
    for index in range(3):
        intermediate = _term(f"Project {index}", f'\"Project {index}\"')
        facts.extend(
            [
                SnapshotFact(index * 2 + 1, _term("Ada", '\"Ada\"'), assigned, intermediate, "confirmed"),
                SnapshotFact(index * 2 + 2, intermediate, purpose, _term(f"Answer {index}", f'\"Answer {index}\"'), "confirmed"),
            ]
        )
    snapshot = QuerySchemaSnapshot(
        **{**_snapshot().__dict__, "join_facts": tuple(facts)}
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_LOOKUP,
        hops=(
            ConjunctiveHop(ConjunctiveEndpoint("entity", "Ada"), IntentTarget("relation", "assigned_to"), ConjunctiveEndpoint("var", "M")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "M"), IntentTarget("relation", "purpose"), ConjunctiveEndpoint("var", "A")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=18, bounds=QueryPlannerBounds(max_candidates=1))

    assert len(plan.candidates) == 1
    assert plan.truncated is False


def test_conjunctive_filter_uses_only_a_shared_engine_equality_binding():
    role = _term("role", '"role"')
    affiliation = _term("affiliation", '"affiliation"')
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "join_facts": (
                SnapshotFact(1, _term("Ada", '"Ada"'), role, _term("Engineer", '"Engineer"'), "confirmed"),
                SnapshotFact(2, _term("Ada", '"Ada"'), affiliation, _term("Research Team", '"Research Team"'), "confirmed"),
                SnapshotFact(3, _term("Bryn", '"Bryn"'), role, _term("Engineer", '"Engineer"'), "confirmed"),
                SnapshotFact(4, _term("Cato", '"Cato"'), affiliation, _term("Research Team", '"Research Team"'), "confirmed"),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "role"), ConjunctiveEndpoint("entity", "Engineer")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "affiliation"), ConjunctiveEndpoint("entity", "Research Team")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=19)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q19(value: symbol)\n'
        'answer_q19(A) :- relation(A, "affiliation", "Research Team"), relation(A, "role", "Engineer").'
    ]
    assert plan.candidates[0].family is QueryCandidateFamily.CONJUNCTIVE_FILTER


def test_conjunctive_filter_uses_engine_leaf_equality_for_the_shared_binding():
    role = _term("role", '"role"')
    affiliation = _term("affiliation", '"affiliation"')
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "join_facts": (
                SnapshotFact(1, _term("Ada", 'Ada', "Atom"), role, _term("Engineer", '"Engineer"'), "confirmed"),
                SnapshotFact(2, _term("Ada", '"Ada"'), affiliation, _term("Research Team", '"Research Team"'), "confirmed"),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "role"), ConjunctiveEndpoint("entity", "Engineer")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "affiliation"), ConjunctiveEndpoint("entity", "Research Team")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=20)

    assert len(plan.candidates) == 1


def test_conjunctive_filter_bounds_unique_rule_shapes_not_matching_facts():
    role = _term("role", '"role"')
    affiliation = _term("affiliation", '"affiliation"')
    facts = []
    for index in range(3):
        person = _term(f"Person {index}", f'"Person {index}"')
        facts.extend((
            SnapshotFact(index * 2 + 1, person, role, _term("Engineer", '"Engineer"'), "confirmed"),
            SnapshotFact(index * 2 + 2, person, affiliation, _term("Research Team", '"Research Team"'), "confirmed"),
        ))
    snapshot = QuerySchemaSnapshot(**{**_snapshot().__dict__, "join_facts": tuple(facts)})
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "role"), ConjunctiveEndpoint("entity", "Engineer")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "affiliation"), ConjunctiveEndpoint("entity", "Research Team")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=21, bounds=QueryPlannerBounds(max_candidates=1))

    assert len(plan.candidates) == 1
    assert plan.truncated is False


def test_conjunctive_filter_deduplicates_alias_equivalent_rule_shapes_before_candidate_cap():
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "relation_aliases": (
                RelationAliasEntry(alias="role_alpha", canonical="role"),
                RelationAliasEntry(alias="role_beta", canonical="role"),
                RelationAliasEntry(alias="affiliation_alpha", canonical="affiliation"),
                RelationAliasEntry(alias="affiliation_beta", canonical="affiliation"),
            ),
            "join_facts": (
                SnapshotFact(1, _term("Ada", '"Ada"'), _term("role_beta", '"role_beta"'), _term("Engineer", '"Engineer"'), "confirmed"),
                SnapshotFact(2, _term("Ada", '"Ada"'), _term("affiliation_beta", '"affiliation_beta"'), _term("Research Team", '"Research Team"'), "confirmed"),
                SnapshotFact(3, _term("Bryn", '"Bryn"'), _term("role_alpha", '"role_alpha"'), _term("Engineer", '"Engineer"'), "confirmed"),
                SnapshotFact(4, _term("Bryn", '"Bryn"'), _term("affiliation_alpha", '"affiliation_alpha"'), _term("Research Team", '"Research Team"'), "confirmed"),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "role"), ConjunctiveEndpoint("entity", "Engineer")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "affiliation"), ConjunctiveEndpoint("entity", "Research Team")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=22, bounds=QueryPlannerBounds(max_candidates=1))

    assert len(plan.candidates) == 1
    assert plan.truncated is False
    assert plan.candidates[0].query_dl == (
        '.decl answer_q22(value: symbol)\n'
        'answer_q22(A) :- relation(A, "affiliation_beta", "Research Team"), relation(A, "role_beta", "Engineer").'
    )


def test_conjunctive_filter_fails_closed_for_alias_equivalent_conditions():
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "relation_aliases": (RelationAliasEntry(alias="position", canonical="role"),),
            "join_facts": (
                SnapshotFact(
                    1,
                    _term("Ada", '"Ada"'),
                    _term("position", '"position"'),
                    _term("Engineer", '"Engineer"'),
                    "confirmed",
                ),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(
                ConjunctiveEndpoint("var", "A"),
                IntentTarget("relation", "position"),
                ConjunctiveEndpoint("entity", "Engineer"),
            ),
            ConjunctiveHop(
                ConjunctiveEndpoint("var", "A"),
                IntentTarget("relation", "role"),
                ConjunctiveEndpoint("entity", "Engineer"),
            ),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=24)

    assert plan.candidates == ()
    assert plan.truncated is True


def test_conjunctive_filter_truncates_when_join_facts_are_truncated():
    snapshot = QuerySchemaSnapshot(
        **{**_snapshot().__dict__, "join_facts_truncated": True}
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=(
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "role"), ConjunctiveEndpoint("entity", "Engineer")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "A"), IntentTarget("relation", "affiliation"), ConjunctiveEndpoint("entity", "Research Team")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=23)

    assert plan.candidates == ()
    assert plan.truncated is True


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


def test_conjunctive_three_hop_uses_only_a_complete_observed_path():
    owns = _term("owns", '"owns"')
    runs = _term("runs", '"runs"')
    purpose = _term("purpose", '"purpose"')
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "join_facts": (
                SnapshotFact(1, _term("Example Org", '"Example Org"'), owns, _term("Program", '"Program"'), "confirmed"),
                SnapshotFact(2, _term("Program", '"Program"'), runs, _term("Project", '"Project"'), "confirmed"),
                SnapshotFact(3, _term("Project", '"Project"'), purpose, _term("Research", '"Research"'), "confirmed"),
                SnapshotFact(4, _term("Other", '"Other"'), purpose, _term("Unrelated", '"Unrelated"'), "confirmed"),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
        chain_hops=(
            ConjunctiveHop(ConjunctiveEndpoint("entity", "Example Org"), IntentTarget("relation", "owns"), ConjunctiveEndpoint("var", "M")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "M"), IntentTarget("relation", "runs"), ConjunctiveEndpoint("var", "N")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "N"), IntentTarget("relation", "purpose"), ConjunctiveEndpoint("var", "A")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=31)

    assert [candidate.query_dl for candidate in plan.candidates] == [
        '.decl answer_q31(value: symbol)\n'
        'answer_q31(A) :- relation("Example Org", "owns", M), relation(M, "runs", N), relation(N, "purpose", A).'
    ]
    assert plan.candidates[0].family is QueryCandidateFamily.CONJUNCTIVE_THREE_HOP


def test_conjunctive_three_hop_deduplicates_canonical_shapes_and_fails_closed_on_cap():
    snapshot = QuerySchemaSnapshot(
        **{
            **_snapshot().__dict__,
            "relation_aliases": (
                RelationAliasEntry(alias="owns_alpha", canonical="owns"),
                RelationAliasEntry(alias="owns_beta", canonical="owns"),
            ),
            "join_facts": (
                SnapshotFact(1, _term("Example Org", '"Example Org"'), _term("owns_alpha", '"owns_alpha"'), _term("Program One", '"Program One"'), "confirmed"),
                SnapshotFact(2, _term("Program One", '"Program One"'), _term("runs", '"runs"'), _term("Project One", '"Project One"'), "confirmed"),
                SnapshotFact(3, _term("Project One", '"Project One"'), _term("purpose", '"purpose"'), _term("Research", '"Research"'), "confirmed"),
                SnapshotFact(4, _term("Example Org", '"Example Org"'), _term("owns_beta", '"owns_beta"'), _term("Program Two", '"Program Two"'), "confirmed"),
                SnapshotFact(5, _term("Program Two", '"Program Two"'), _term("runs", '"runs"'), _term("Project Two", '"Project Two"'), "confirmed"),
                SnapshotFact(6, _term("Project Two", '"Project Two"'), _term("purpose", '"purpose"'), _term("Delivery", '"Delivery"'), "confirmed"),
                SnapshotFact(7, _term("Example Org", '"Example Org"'), _term("operates", '"operates"'), _term("Program Three", '"Program Three"'), "confirmed"),
                SnapshotFact(8, _term("Program Three", '"Program Three"'), _term("runs", '"runs"'), _term("Project Three", '"Project Three"'), "confirmed"),
                SnapshotFact(9, _term("Project Three", '"Project Three"'), _term("purpose", '"purpose"'), _term("Other", '"Other"'), "confirmed"),
            ),
        }
    )
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
        chain_hops=(
            ConjunctiveHop(ConjunctiveEndpoint("entity", "Example Org"), IntentTarget("relation", "owns"), ConjunctiveEndpoint("var", "M")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "M"), IntentTarget("relation", "runs"), ConjunctiveEndpoint("var", "N")),
            ConjunctiveHop(ConjunctiveEndpoint("var", "N"), IntentTarget("relation", "purpose"), ConjunctiveEndpoint("var", "A")),
        ),
        answer_var="A",
    )

    plan = plan_query_candidates(intent, snapshot, qid=32, bounds=QueryPlannerBounds(max_candidates=1))

    assert len(plan.candidates) == 1
    assert plan.truncated is False
    truncated_snapshot = QuerySchemaSnapshot(**{**snapshot.__dict__, "join_facts_truncated": True})
    assert plan_query_candidates(intent, truncated_snapshot, qid=32).truncated is True


def test_typed_comparison_plans_exact_number_operators_at_boundaries():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    relation = _term("metric", '"metric"')
    facts = tuple(
        SnapshotFact(index, subject, relation, _term(f"number({value})", f"number({value})", "Compound"), "confirmed", "Synthetic Company", "subject")
        for index, value in enumerate(("9.999", "10", "10.001"), start=1)
    )
    snapshot = _typed_exact_snapshot(
        facts,
        typed=(TypedRelationEntry("metric", "number", "metric_scalar"),),
    )

    expected = {
        "=": ("number(10)",),
        "!=": ("number(9.999)", "number(10.001)"),
        "<": ("number(9.999)",),
        "<=": ("number(9.999)", "number(10)"),
        ">": ("number(10.001)",),
        ">=": ("number(10)", "number(10.001)"),
    }
    for operator, values in expected.items():
        plan = plan_query_candidates(_compare_intent(operator=operator), snapshot, qid=41)

        assert len(plan.candidates) == 1
        assert all(f"answer_q41({value})" in plan.candidates[0].query_dl for value in values)


def test_typed_comparison_requires_full_dates_and_exact_configured_amount_units():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    date_relation = _term("released_on", '"released_on"')
    amount_relation = _term("inventory_value", '"inventory_value"')
    facts = (
        SnapshotFact(1, subject, date_relation, _term("date(2024, 7, 3)", "date(2024, 7, 3)", "Compound"), "confirmed", "Synthetic Company", "subject"),
        SnapshotFact(2, subject, amount_relation, _term('amount(1.5, "crate")', 'amount(1.5, "crate")', "Compound"), "confirmed", "Synthetic Company", "subject"),
    )
    snapshot = _typed_exact_snapshot(
        facts,
        typed=(
            TypedRelationEntry("released_on", "date", "release_date"),
            TypedRelationEntry("inventory_value", "amount", "inventory_scalar", ()),
        ),
    )
    date_plan = plan_query_candidates(
        _compare_intent(relation="released_on", value_type="date", value="date(2024, 7, 3)"),
        snapshot,
        qid=42,
    )
    assert len(date_plan.candidates) == 1
    assert not plan_query_candidates(
        _compare_intent(relation="released_on", value_type="date", value="date(2024, 7)"),
        snapshot,
        qid=42,
    ).candidates

    # Amount comparison accepts only a configured unit and only when Decimal *
    # scale is an exact integral base-unit amount.
    configured = QuerySchemaSnapshot(
        **{
            **snapshot.__dict__,
            "typed_relations": (
                TypedRelationEntry(
                    "inventory_value", "amount", "inventory_scalar",
                    units=(UnitScale("crate", 10),),
                ),
            ),
        }
    )
    assert len(plan_query_candidates(
        _compare_intent(relation="inventory_value", value_type="amount", value='amount(1.5, "crate")'),
        configured,
        qid=42,
    ).candidates) == 1
    assert not plan_query_candidates(
        _compare_intent(relation="inventory_value", value_type="amount", value='amount(1.1, "crate")'),
        QuerySchemaSnapshot(**{**configured.__dict__, "typed_relations": (TypedRelationEntry("inventory_value", "amount", "inventory_scalar", units=(UnitScale("crate", 3),)),)}),
        qid=42,
    ).candidates
    assert not plan_query_candidates(
        _compare_intent(relation="inventory_value", value_type="amount", value='amount(1, "unknown")'),
        configured,
        qid=42,
    ).candidates


def test_typed_comparison_groups_alias_facts_and_fails_closed_for_invalid_or_truncated_data():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    alias_relation = _term("gross_sales", '"gross_sales"')
    canonical_relation = _term("revenue", '"revenue"')
    good_facts = (
        SnapshotFact(1, subject, alias_relation, _term("number(11)", "number(11)", "Compound"), "confirmed", "Synthetic Company", "subject"),
        SnapshotFact(2, subject, canonical_relation, _term("number(12)", "number(12)", "Compound"), "confirmed", "Synthetic Company", "subject"),
    )
    snapshot = _typed_exact_snapshot(
        good_facts,
        typed=(TypedRelationEntry("revenue", "number", "revenue_scalar"),),
        aliases=(RelationAliasEntry("gross_sales", "revenue"),),
    )
    plan = plan_query_candidates(_compare_intent(relation="gross_sales"), snapshot, qid=43)

    assert len(plan.candidates) == 1
    assert plan.candidates[0].relation_display == "revenue"
    assert 'relation("Synthetic Company", "gross_sales", number(11))' in plan.candidates[0].query_dl
    assert 'relation("Synthetic Company", "revenue", number(12))' in plan.candidates[0].query_dl

    # An untyped relation and a truncated fact slice must not produce a candidate
    # that looks complete.
    untyped = QuerySchemaSnapshot(**{**snapshot.__dict__, "typed_relations": ()})
    assert not plan_query_candidates(_compare_intent(relation="gross_sales"), untyped, qid=43).candidates
    assert plan_query_candidates(_compare_intent(relation="gross_sales"), QuerySchemaSnapshot(**{**snapshot.__dict__, "exact_entity_facts_truncated": True}), qid=43).truncated is True


def test_typed_comparison_ordinal_rejects_nonordinal_facts_and_overflow():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    relation = _term("rank", '"rank"')
    snapshot = _typed_exact_snapshot(
        (SnapshotFact(1, subject, relation, _term("제3호", '"제3호"'), "confirmed", "Synthetic Company", "subject"),),
        typed=(TypedRelationEntry("rank", "ordinal", "rank_number"),),
    )

    assert len(plan_query_candidates(
        _compare_intent(relation="rank", value_type="ordinal", operator="=", value="ordinal(3)"),
        snapshot,
        qid=44,
    ).candidates) == 1
    assert not plan_query_candidates(
        _compare_intent(relation="rank", value_type="ordinal", value="ordinal(999999999999999999999)"),
        snapshot,
        qid=44,
    ).candidates


def test_typed_comparison_with_valid_nonmatching_facts_has_explicit_no_answer_plan():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    relation = _term("metric", '"metric"')
    snapshot = _typed_exact_snapshot(
        (
            SnapshotFact(
                1,
                subject,
                relation,
                _term("number(9)", "number(9)", "Compound"),
                "confirmed",
                "Synthetic Company",
                "subject",
            ),
        ),
        typed=(TypedRelationEntry("metric", "number", "metric_scalar"),),
    )

    plan = plan_query_candidates(_compare_intent(operator=">", value="number(10)"), snapshot, qid=45)

    assert plan.candidates == ()
    assert plan.no_answer is True
    assert plan.reason is None


def test_typed_comparison_malformed_requested_relation_aborts_other_candidates():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    revenue = _term("revenue", '"revenue"')
    cost = _term("cost", '"cost"')
    snapshot = _typed_exact_snapshot(
        (
            SnapshotFact(1, subject, revenue, _term("number(20)", "number(20)", "Compound"), "confirmed", "Synthetic Company", "subject"),
            SnapshotFact(2, subject, cost, _term("number(not-a-number)", "number(not-a-number)", "Compound"), "confirmed", "Synthetic Company", "subject"),
        ),
        typed=(
            TypedRelationEntry("revenue", "number", "revenue_scalar"),
            TypedRelationEntry("cost", "number", "cost_scalar"),
        ),
    )

    plan = plan_query_candidates(
        _compare_intent(relation="revenue", relation_candidates=("cost",)),
        snapshot,
        qid=46,
    )

    assert plan.candidates == ()
    assert plan.no_answer is False
    assert plan.reason == "typed comparison has malformed or uncomparable evidence"


def test_typed_comparison_type_mismatch_aborts_instead_of_returning_no_answer():
    subject = _term("Synthetic Company", '"Synthetic Company"')
    revenue = _term("revenue", '"revenue"')
    released_on = _term("released_on", '"released_on"')
    snapshot = _typed_exact_snapshot(
        (
            SnapshotFact(1, subject, revenue, _term("number(9)", "number(9)", "Compound"), "confirmed", "Synthetic Company", "subject"),
            SnapshotFact(2, subject, released_on, _term("date(2024, 7, 3)", "date(2024, 7, 3)", "Compound"), "confirmed", "Synthetic Company", "subject"),
        ),
        typed=(
            TypedRelationEntry("revenue", "number", "revenue_scalar"),
            TypedRelationEntry("released_on", "date", "release_date"),
        ),
    )

    plan = plan_query_candidates(
        _compare_intent(relation="revenue", relation_candidates=("released_on",)),
        snapshot,
        qid=47,
    )

    assert plan.candidates == ()
    assert plan.no_answer is False
    assert plan.reason == "typed comparison relation has incompatible typed spec"
