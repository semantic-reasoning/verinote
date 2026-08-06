# SPDX-License-Identifier: MPL-2.0
from dataclasses import FrozenInstanceError

import pytest

from verinote.pipeline.query_intent import (
    ConjunctiveEndpoint,
    ConjunctiveHop,
    ENGLISH_ROLE_RELATION_CANDIDATES,
    KOREAN_ROLE_RELATION_CANDIDATES,
    PURPOSE_RELATION_CANDIDATES,
    IntentTarget,
    QueryIntent,
    QueryIntentKind,
    deterministic_query_intent,
    parse_query_intent,
)


def test_conjunctive_lookup_requires_one_connected_two_hop_chain():
    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_LOOKUP,
        hops=(
            ConjunctiveHop(
                ConjunctiveEndpoint("entity", "Ada"),
                IntentTarget("relation", "assigned_to"),
                ConjunctiveEndpoint("var", "M"),
            ),
            ConjunctiveHop(
                ConjunctiveEndpoint("var", "M"),
                IntentTarget("relation", "purpose"),
                ConjunctiveEndpoint("var", "A"),
            ),
        ),
        answer_var="A",
    )

    assert intent.kind is QueryIntentKind.CONJUNCTIVE_LOOKUP
    with pytest.raises(ValueError, match="intermediate"):
        QueryIntent(
            kind=QueryIntentKind.CONJUNCTIVE_LOOKUP,
            hops=(
                intent.hops[0],
                ConjunctiveHop(
                    ConjunctiveEndpoint("var", "X"),
                    IntentTarget("relation", "purpose"),
                    ConjunctiveEndpoint("var", "A"),
                ),
            ),
            answer_var="A",
        )


def test_parse_conjunctive_lookup_rejects_incomplete_or_disconnected_hops():
    payload = {
        "kind": "conjunctive_lookup",
        "subject": None,
        "relation": None,
        "object": None,
        "relation_candidates": None,
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": None,
        "hops": [
            {
                "subject": {"kind": "entity", "value": "Ada"},
                "relation": {"kind": "relation", "value": "assigned_to"},
                "object": {"kind": "var", "value": "M"},
            },
            {
                "subject": {"kind": "var", "value": "M"},
                "relation": {"kind": "relation", "value": "purpose"},
                "object": {"kind": "var", "value": "A"},
            },
        ],
        "answer_var": "A",
    }

    assert parse_query_intent(payload).answer_var == "A"
    payload["hops"][1]["subject"]["value"] = "X"
    with pytest.raises(Exception, match="intermediate"):
        parse_query_intent(payload)


def test_conjunctive_filter_requires_two_conditions_on_one_answer_variable():
    conditions = (
        ConjunctiveHop(
            ConjunctiveEndpoint("var", "A"),
            IntentTarget("relation", "role"),
            ConjunctiveEndpoint("entity", "Engineer"),
        ),
        ConjunctiveHop(
            ConjunctiveEndpoint("entity", "Research Team"),
            IntentTarget("relation", "affiliation"),
            ConjunctiveEndpoint("var", "A"),
        ),
    )

    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_FILTER,
        conditions=conditions,
        answer_var="A",
    )

    assert intent.conditions == conditions
    with pytest.raises(ValueError, match="additional variables"):
        QueryIntent(
            kind=QueryIntentKind.CONJUNCTIVE_FILTER,
            conditions=(
                conditions[0],
                ConjunctiveHop(
                    ConjunctiveEndpoint("var", "B"),
                    IntentTarget("relation", "affiliation"),
                    ConjunctiveEndpoint("entity", "Research Team"),
                ),
            ),
            answer_var="A",
        )


def test_parse_conjunctive_filter_rejects_incomplete_conditions():
    payload = {
        "kind": "conjunctive_filter",
        "subject": None,
        "relation": None,
        "object": None,
        "relation_candidates": None,
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": None,
        "hops": None,
        "conditions": [
            {
                "subject": {"kind": "var", "value": "A"},
                "relation": {"kind": "relation", "value": "role"},
                "object": {"kind": "entity", "value": "Engineer"},
            }
        ],
        "answer_var": "A",
    }

    with pytest.raises(Exception, match="exactly two conditions"):
        parse_query_intent(payload)


def test_conjunctive_three_hop_lookup_requires_one_forward_chain():
    hops = (
        ConjunctiveHop(
            ConjunctiveEndpoint("entity", "Example Org"),
            IntentTarget("relation", "owns"),
            ConjunctiveEndpoint("var", "M"),
        ),
        ConjunctiveHop(
            ConjunctiveEndpoint("var", "M"),
            IntentTarget("relation", "runs"),
            ConjunctiveEndpoint("var", "N"),
        ),
        ConjunctiveHop(
            ConjunctiveEndpoint("var", "N"),
            IntentTarget("relation", "purpose"),
            ConjunctiveEndpoint("var", "A"),
        ),
    )

    intent = QueryIntent(
        kind=QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
        chain_hops=hops,
        answer_var="A",
    )

    assert intent.chain_hops == hops
    with pytest.raises(ValueError, match="must be distinct"):
        QueryIntent(
            kind=QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
            chain_hops=(
                hops[0],
                hops[1],
                ConjunctiveHop(
                    ConjunctiveEndpoint("var", "N"),
                    IntentTarget("relation", "purpose"),
                    ConjunctiveEndpoint("var", "N"),
                ),
            ),
            answer_var="N",
        )


def test_parse_conjunctive_three_hop_lookup_rejects_disconnected_chain():
    payload = {
        "kind": "conjunctive_three_hop_lookup",
        "subject": None,
        "relation": None,
        "object": None,
        "relation_candidates": None,
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": None,
        "hops": None,
        "conditions": None,
        "chain_hops": [
            {"subject": {"kind": "entity", "value": "Example Org"}, "relation": {"kind": "relation", "value": "owns"}, "object": {"kind": "var", "value": "M"}},
            {"subject": {"kind": "var", "value": "M"}, "relation": {"kind": "relation", "value": "runs"}, "object": {"kind": "var", "value": "N"}},
            {"subject": {"kind": "var", "value": "X"}, "relation": {"kind": "relation", "value": "purpose"}, "object": {"kind": "var", "value": "A"}},
        ],
        "answer_var": "A",
    }

    with pytest.raises(Exception, match="second and third"):
        parse_query_intent(payload)


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


def test_valid_lookup_subject_lookup_relation_and_compare_intents():
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
    # A reason or a stray comparison field alongside a valid classification is
    # advisory, not a violation (#237). Nothing outside this module reads
    # operator/value_type/value, so tolerating them cannot change the query;
    # a bad *value* (value_type="duration" above) is still rejected.
    advisory = QueryIntent(
        kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
        subject=IntentTarget("entity", "Sample Entity"),
        operator=">",
        reason="the entity is named but the relation is open",
    )
    assert advisory.operator == ">"
    assert advisory.reason == "the entity is named but the relation is open"


def test_intent_rejects_off_schema_comparison_values_on_any_kind():
    """A stray comparison field is ignored only while it stays on-schema.

    `operator: "="` on a lookup_object is schema-legal, so tolerating it is the
    #237 fix. `operator: "contains"` is not in QUERY_INTENT_SCHEMA's enum at all,
    so accepting it would put the validator's boundary outside the schema's --
    admitting output no strict-mode provider could even produce.
    """
    with pytest.raises(ValueError, match="operator"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_OBJECT,
            subject=IntentTarget("entity", "샘플인물"),
            relation=IntentTarget("relation", "역할"),
            operator="contains",
        )
    with pytest.raises(ValueError, match="value_type"):
        QueryIntent(
            kind=QueryIntentKind.LOOKUP_OBJECT,
            subject=IntentTarget("entity", "샘플인물"),
            relation=IntentTarget("relation", "역할"),
            value_type="duration",
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


def test_generic_attribute_questions_become_lookup_object_intents():
    korean = deterministic_query_intent("샘플프로젝트의 목적은?")
    korean_explicit = deterministic_query_intent("샘플프로젝트의 목적은 무엇인가?")
    english_possessive = deterministic_query_intent("What is Sample Project's purpose?")
    english_of = deterministic_query_intent("What is the purpose of Sample Project?")

    for intent in (korean, korean_explicit, english_possessive, english_of):
        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
        # The Korean forms also carry their un-stripped josa reading (#431);
        # English has no josa, so those stay exactly the synonym set.
        assert intent.relation_candidates[: len(PURPOSE_RELATION_CANDIDATES)] == (
            PURPOSE_RELATION_CANDIDATES
        )
    assert english_possessive.relation_candidates == PURPOSE_RELATION_CANDIDATES
    assert english_of.relation_candidates == PURPOSE_RELATION_CANDIDATES
    assert korean.relation_candidates == PURPOSE_RELATION_CANDIDATES + ("목적은",)
    assert korean_explicit.relation_candidates == PURPOSE_RELATION_CANDIDATES + (
        "목적은",
    )
    assert korean.subject == IntentTarget("entity", "샘플프로젝트")
    assert english_possessive.subject == IntentTarget("entity", "Sample Project")
    assert english_of.subject == IntentTarget("entity", "Sample Project")


def test_generic_korean_attribute_requires_question_shape():
    intent = deterministic_query_intent("샘플프로젝트의 목적")

    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED


# Built as an actual cross-product rather than a hand-written list, so that
# every added stem really is pinned against every suffix form the rule admits.
# A hand-list drifts: with `이에요` written out for one stem only, dropping it
# from the other three fails no test, because the stem that still carries it
# masks them.
# The second entry of each pair is the un-stripped josa reading, offered
# alongside the stripped one because nothing here can tell a josa from a
# label's own last syllable (#431). It never matches for these labels; the
# stripped reading is the one the KB holds.
_KOREAN_INTERROGATIVE_STEMS = (
    ("샘플프로젝트의 담당자는 누구", ("담당자", "담당자는")),
    ("샘플제품의 가격은 얼마", ("가격", "가격은")),
    ("샘플조직의 본사는 어디", ("본사", "본사는")),
    ("샘플프로젝트의 착수일은 언제", ("착수일", "착수일은")),
)
_KOREAN_INTERROGATIVE_QUESTION_FORMS = (
    "인가?",
    "인가요?",
    "입니까?",
    "예요?",
    "이에요?",
    "야?",
    "?",
)


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        (stem + form, expected)
        for stem, expected in _KOREAN_INTERROGATIVE_STEMS
        for form in _KOREAN_INTERROGATIVE_QUESTION_FORMS
    ]
    # The pre-existing stems keep working, including the forms this rule newly
    # admits on each of them. They are listed separately because they do not
    # carry the same suffix set as the four added stems.
    + [
        ("샘플프로젝트의 목적은 무엇인가요?", PURPOSE_RELATION_CANDIDATES + ("목적은",)),
        ("샘플프로젝트의 목적이 뭐인가요?", PURPOSE_RELATION_CANDIDATES + ("목적이",)),
        ("샘플프로젝트의 목적이 뭐예요?", PURPOSE_RELATION_CANDIDATES + ("목적이",)),
        ("샘플문서의 형식은 어떤 것인가요?", ("형식", "형식은")),
    ],
)
def test_korean_attribute_questions_strip_person_place_time_and_amount_words(
    question, candidates
):
    """`누구`/`얼마`/`어디`/`언제` are interrogatives, not part of the relation.

    Stripping only `무엇` left the relation candidate as the entire phrase (e.g.
    `담당자는 누구`), which no schema can hold, so a question that named its
    relation exactly still planned no candidates and was answered UNVERIFIED.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


@pytest.mark.parametrize("relation", ["개요", "분야"])
def test_korean_attribute_label_keeps_a_relation_whose_last_syllable_is_an_ending(
    relation,
):
    """The interrogative strip must not eat a syllable a relation name owns.

    `개요` and `분야` end in `요` and `야`, the two endings the added stems carry
    that are also ordinary final syllables. Admitting either as an alternative
    that can match with an empty stem would ask the schema for `개` or `분`, and
    the question would stop matching its own relation.

    Only the bare form is asserted. Behind a josa the label is protected anyway
    -- in `개요는 무엇인가요?` the `무엇인가요` wins the end-of-string anchor --
    so that form cannot fail and would be coverage in name only.
    """
    assert deterministic_query_intent(f"샘플문서의 {relation}?").relation_candidates == (
        relation,
    )


def test_korean_attribute_label_does_not_strip_a_stemless_politeness_ending():
    """The stemless `인가`/`입니까` alternatives must stay unwidened.

    They are the only alternatives that match with no interrogative stem in
    front, so giving either one an optional `요` puts the whole rule back in the
    hazard the stem-bound suffixes avoid: `재인가요` would lose four syllables
    and ask the schema for `재`.
    """
    assert deterministic_query_intent("샘플사업의 재인가요?").relation_candidates == (
        "재인가요",
    )
    # The form a KB holding `재인가` actually answers keeps working, because the
    # `는` here is a real josa rather than a politeness ending.
    assert deterministic_query_intent("샘플사업의 재인가는?").relation_candidates == (
        "재인가",
        "재인가는",
    )


def test_deterministic_entity_relation_discovery_questions_are_generic():
    english = deterministic_query_intent("How is Sample Entity related?")
    connected = deterministic_query_intent(
        "Which relation connects Sample Entity to other facts?"
    )
    direct_hint = deterministic_query_intent("What does Sample Entity provide?")
    korean = deterministic_query_intent("샘플엔티티는 어떤 관계인가?")
    korean_direct_hint = deterministic_query_intent("샘플엔티티가 제공하는 것은?")

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
    assert korean_direct_hint.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert korean_direct_hint.subject == IntentTarget("entity", "샘플엔티티")
    assert korean_direct_hint.relation == IntentTarget("relation", "제공")
    assert korean_direct_hint.relation_candidates == ()


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


@pytest.mark.parametrize(
    "label",
    ["단가", "물가", "증가", "평가", "나이", "차이", "길이", "넓이", "허가", "기준단가"],
)
def test_a_labels_own_last_syllable_survives_as_one_reading(label):
    """A trailing `은`/`는`/`이`/`가` may belong to the relation, not the grammar.

    Stripping it unconditionally asked the schema for `단`, `길`, `나` -- labels
    no KB holds -- so a question naming its relation exactly was answered
    UNVERIFIED. The full spelling is now offered as a reading, and the schema
    decides which one exists.
    """
    intent = deterministic_query_intent(f"샘플대상의 {label}?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert label in intent.relation_candidates


@pytest.mark.parametrize(
    ("question", "label"),
    [
        ("샘플대상의 성과 지표은?", "성과 지표"),
        ("샘플대상의 가격는?", "가격"),
        ("샘플대상의 담당자은?", "담당자"),
        ("샘플제품의 길이 얼마인가?", "길이"),
    ],
)
def test_the_stripped_reading_survives_too(question, label):
    """Keeping the full spelling must not cost the stripped one.

    The first two questions carry a josa that does not agree with the word
    before it -- `지표은` should be `지표는`, `가격는` should be `가격은` -- which
    a person mistypes and a caller templating `{relation}은?` produces
    mechanically. Committing to the full reading there would ask for a relation
    named `가격는`. Both readings are offered precisely because neither guess is
    safe on its own.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert label in intent.relation_candidates


@pytest.mark.parametrize("question", ["샘플프로젝트의 목적는?", "샘플프로젝트의 목적이?"])
def test_the_synonym_set_survives_the_second_reading(question):
    """Adding a second reading must not cost the synonyms the first one carries.

    `목적는` is a mis-typed `목적`, and the stripped reading is the one that
    resolves to the purpose set. Only that reading is expanded -- the second
    cannot be a synonym key today -- so this pins that the set still arrives.
    """
    intent = deterministic_query_intent(question)

    for synonym in PURPOSE_RELATION_CANDIDATES:
        assert synonym in intent.relation_candidates


def test_a_label_that_is_only_a_josa_is_not_claimed():
    """`{entity}의 {label}은?` with a blank label is not an attribute question.

    Reading a bare `은` as a relation name would claim a shape this parser has
    always declined, and then tell the user to add a `policy/relation-aliases.md`
    entry for a grammatical particle -- advice about their own KB that is simply
    wrong. Declining sends it to the model instead, which sees the schema hint,
    so a KB that really does hold a relation named `은` is still reachable.
    """
    for question in ("샘플조직의 은?", "샘플조직의 은 무엇인가요?", "샘플조직의 가?"):
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
        assert intent.relation_candidates == ()

    # Pinned on the function too, not only through the call site's truthiness
    # check: a label that is only a josa has no readings, rather than a reading
    # that happens to be the empty string.
    from verinote.pipeline.query_intent import _korean_attribute_label_readings

    assert _korean_attribute_label_readings("은") == ()
    assert _korean_attribute_label_readings("가") == ()

    # The boundary: one syllable of label before the josa is a label, so `이는?`
    # is an ordinary two-reading question rather than a declined one.
    boundary = deterministic_query_intent("샘플조직의 이는?")
    assert boundary.kind == QueryIntentKind.LOOKUP_OBJECT
    assert boundary.relation_candidates == ("이", "이는")
