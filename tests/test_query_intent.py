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
