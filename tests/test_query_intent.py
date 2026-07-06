# SPDX-License-Identifier: MPL-2.0
from dataclasses import FrozenInstanceError

import pytest

from verinote.pipeline.query_intent import (
    ENGLISH_ROLE_RELATION_CANDIDATES,
    KOREAN_ROLE_RELATION_CANDIDATES,
    IntentTarget,
    QueryIntent,
    QueryIntentKind,
    deterministic_query_intent,
)


def test_lookup_object_intent_is_frozen_and_typed():
    intent = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=IntentTarget("entity", "샘플인물"),
        relation_candidates=("역할", "직책"),
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.subject == IntentTarget("entity", "샘플인물")
    assert intent.relation_candidates == ("역할", "직책")
    with pytest.raises(FrozenInstanceError):
        intent.relation_candidates = ("role",)


def test_valid_lookup_subject_lookup_relation_count_and_compare_intents():
    assert QueryIntent(
        kind=QueryIntentKind.LOOKUP_SUBJECT,
        relation=IntentTarget("relation", "역할"),
        object=IntentTarget("entity", "검토자"),
    ).kind == QueryIntentKind.LOOKUP_SUBJECT
    assert QueryIntent(
        kind=QueryIntentKind.LOOKUP_RELATION,
        subject=IntentTarget("entity", "샘플인물"),
        object=IntentTarget("entity", "샘플문서"),
    ).kind == QueryIntentKind.LOOKUP_RELATION
    assert QueryIntent(
        kind=QueryIntentKind.COUNT,
        relation=IntentTarget("relation", "참여"),
    ).kind == QueryIntentKind.COUNT
    assert QueryIntent(
        kind=QueryIntentKind.COMPARE_TYPED_VALUE,
        subject=IntentTarget("entity", "샘플항목"),
        relation=IntentTarget("relation", "수량"),
        operator=">=",
        value_type="number",
        value="3",
    ).kind == QueryIntentKind.COMPARE_TYPED_VALUE


def test_valid_entity_relation_discovery_intents():
    broad = QueryIntent(
        kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
        subject=IntentTarget("entity", "Sample Entity"),
    )
    direct_first = QueryIntent(
        kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
        subject=IntentTarget("entity", "Sample Entity"),
        relation=IntentTarget("relation", "provides"),
    )
    candidate_first = QueryIntent(
        kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
        subject=IntentTarget("entity", "샘플엔티티"),
        relation_candidates=("제공", "연결"),
    )

    assert broad.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert broad.subject == IntentTarget("entity", "Sample Entity")
    assert broad.relation is None
    assert direct_first.relation == IntentTarget("relation", "provides")
    assert candidate_first.relation_candidates == ("제공", "연결")


def test_intent_rejects_invalid_combinations():
    with pytest.raises(ValueError, match="lookup_object"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_OBJECT,
            subject=IntentTarget("entity", "샘플인물"),
        )
    with pytest.raises(ValueError, match="unknown_or_unsupported"):
        QueryIntent(
            kind=QueryIntentKind.UNKNOWN_OR_UNSUPPORTED,
            subject=IntentTarget("entity", "샘플인물"),
            reason="unsupported",
        )


def test_intent_rejects_swapped_target_kinds_and_bad_value_type():
    with pytest.raises(ValueError, match="lookup_object subject"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_OBJECT,
            subject=IntentTarget("relation", "역할"),
            relation=IntentTarget("relation", "역할"),
        )
    with pytest.raises(ValueError, match="lookup_subject relation"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_SUBJECT,
            relation=IntentTarget("entity", "샘플인물"),
            object=IntentTarget("entity", "검토자"),
        )
    with pytest.raises(ValueError, match="lookup_relation does not accept a relation"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_RELATION,
            subject=IntentTarget("entity", "샘플인물"),
            relation=IntentTarget("relation", "역할"),
        )
    with pytest.raises(ValueError, match="value_type"):
        QueryIntent(
            kind=QueryIntentKind.COMPARE_TYPED_VALUE,
            subject=IntentTarget("entity", "샘플항목"),
            relation=IntentTarget("relation", "수량"),
            operator=">",
            value_type="duration",
            value="10",
        )
    with pytest.raises(ValueError, match="discover_entity_relations"):
        QueryIntent(kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS)
    with pytest.raises(ValueError, match="discover_entity_relations subject"):
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("relation", "제공"),
        )
    with pytest.raises(ValueError, match="discover_entity_relations"):
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
            object=IntentTarget("entity", "Sample Object"),
        )
    with pytest.raises(ValueError, match="does not accept comparison fields"):
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
            operator=">",
        )
    with pytest.raises(ValueError, match="does not accept reason"):
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
            reason="unsupported",
        )
    with pytest.raises(ValueError, match="relation or relation_candidates"):
        QueryIntent(
            kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
            subject=IntentTarget("entity", "Sample Entity"),
            relation=IntentTarget("relation", "provides"),
            relation_candidates=("offers",),
        )


def test_unsupported_deterministic_question_returns_unknown_intent():
    intent = deterministic_query_intent("이 질문은 합성이지만 지원하지 않는 형태입니다.")

    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    assert intent.reason == "unsupported deterministic query shape"


def test_korean_role_title_questions_preserve_source_language_candidates():
    for label in ("역할", "직책", "직위"):
        intent = deterministic_query_intent(f"샘플인물의 {label}은 무엇인가?")

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
        assert intent.subject == IntentTarget("entity", "샘플인물")
        assert intent.relation_candidates == KOREAN_ROLE_RELATION_CANDIDATES
        assert "role" not in intent.relation_candidates


def test_english_role_title_questions_use_english_candidates():
    intent = deterministic_query_intent("What is Sample Person's role?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.subject == IntentTarget("entity", "Sample Person")
    assert intent.relation_candidates == ENGLISH_ROLE_RELATION_CANDIDATES


def test_deterministic_entity_relation_discovery_questions_are_generic():
    english = deterministic_query_intent("How is Sample Entity related?")
    connected = deterministic_query_intent(
        "Which relation connects Sample Entity to other facts?"
    )
    direct_hint = deterministic_query_intent("What does Sample Entity provide?")
    korean = deterministic_query_intent("샘플엔티티는 어떤 관계인가?")

    assert english.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert english.subject == IntentTarget("entity", "Sample Entity")
    assert english.relation_candidates == ()
    assert connected.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert connected.subject == IntentTarget("entity", "Sample Entity")
    assert direct_hint.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert direct_hint.subject == IntentTarget("entity", "Sample Entity")
    assert direct_hint.relation == IntentTarget("relation", "provide")
    lower_direct_hint = deterministic_query_intent("what does Sample Entity provide?")
    assert lower_direct_hint.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert lower_direct_hint.relation == IntentTarget("relation", "provide")
    assert korean.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert korean.subject == IntentTarget("entity", "샘플엔티티")


def test_deterministic_entity_relation_discovery_rejects_generic_what_does_shapes():
    assert (
        deterministic_query_intent("how is this related?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    assert (
        deterministic_query_intent("How is This related?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    assert (
        deterministic_query_intent("What does sample entity provide?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    assert (
        deterministic_query_intent("What does This provide?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    assert (
        deterministic_query_intent("What does Sample Entity mean?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
    assert (
        deterministic_query_intent("What does Sample Entity have?").kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    )
