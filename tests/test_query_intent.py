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


# Two lists, not one, and not only for grammaticality (`개이야` is not Korean).
# The halves pin different alternatives: the vowel counters are the only thing
# here that pins the bare `야`, whereas `이야` is pinned redundantly -- these
# counters, the counterless `몇이야?` below and the spaced-off `몇 살 이야?`
# each kill its removal. A single merged list would let one mask the other.
_KOREAN_MEASURE_BATCHIM_COUNTERS = ("살", "명", "건", "년", "원", "시간")
_KOREAN_MEASURE_BATCHIM_FORMS = ("인가?", "인가요?", "입니까?", "이야?", "이에요?", "?")
_KOREAN_MEASURE_VOWEL_COUNTERS = ("개", "차", "배", "가지", "퍼센트", "회")
_KOREAN_MEASURE_VOWEL_FORMS = ("인가?", "인가요?", "입니까?", "야?", "예요?", "?")


@pytest.mark.parametrize(
    "question",
    [
        f"샘플인물의 나이는 몇 {counter}{form}"
        for counter in _KOREAN_MEASURE_BATCHIM_COUNTERS
        for form in _KOREAN_MEASURE_BATCHIM_FORMS
    ],
)
def test_a_counted_measure_question_asks_for_the_relation_it_names(question):
    """`몇` plus a counter noun is the question, not part of the relation.

    `샘플인물의 나이는 몇 살인가?` asked the schema for a relation named
    `나이는 몇 살`, which no schema holds, so a question naming its relation
    exactly was answered UNVERIFIED. Built as a real cross-product so that one
    counter carrying a form cannot mask that form's absence on the others.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("나이", "나이는")


@pytest.mark.parametrize(
    "question",
    [
        f"샘플인물의 나이는 몇 {counter}{form}"
        for counter in _KOREAN_MEASURE_VOWEL_COUNTERS
        for form in _KOREAN_MEASURE_VOWEL_FORMS
    ],
)
def test_a_vowel_final_counter_takes_the_vowel_final_predicates(question):
    """The same rule, on the counters that take `야`/`예요` rather than `이야`.

    These cells are the only ones that pin the bare `야` alternative; the
    받침 counters pin `이야`. Asserting both sets on one counter list would
    put ungrammatical Korean (`개이야`) in the fixture.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("나이", "나이는")


@pytest.mark.parametrize("question", ["샘플인물의 나이는 몇인가?", "샘플인물의 나이는 몇이야?", "샘플인물의 나이는 몇?"])
def test_a_measure_question_needs_no_counter(question):
    """`몇` alone is a measure question -- the counter is optional.

    Requiring one would leave `나이는 몇` as the relation candidate, the same
    label no schema holds that the counted forms already fail on.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("나이", "나이는")


def test_a_measure_question_may_omit_the_space_before_the_counter():
    """`몇살인가?` is how the form is commonly typed.

    The space is optional between the interrogative and its counter. After a
    Hangul syllable it is not optional before the interrogative itself -- see
    the word-boundary tests.
    """
    intent = deterministic_query_intent("샘플인물의 나이는 몇살인가?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("나이", "나이는")


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        ("샘플인물의 나이는 몇 살 인가?", ("나이", "나이는")),
        ("샘플인물의 나이는 몇 살 이야?", ("나이", "나이는")),
        ("샘플대상의 수량은 몇 개 인가요?", ("수량", "수량은")),
        ("샘플사업의 기간은 몇 년 입니까?", ("기간", "기간은")),
    ],
)
def test_a_measure_question_may_space_the_predicate_off_the_counter(
    question, candidates
):
    """The space *after* the counter is optional too, and separately so.

    Its sibling above pins the space before the counter. Nothing else pins this
    one: without it the measure tail stops reaching the end anchor on every one
    of these, and each falls back to naming a relation that still carries its
    counter -- `나이는 몇 살`, `수량은 몇 개 인가요` -- which is the defect this
    rule exists to fix, in the spelling a writer gets by putting a space where
    Korean allows one.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


@pytest.mark.parametrize(
    "question",
    [
        "샘플사업의 기간은 얼마나 되나요?",
        "샘플사업의 기간은 얼마나 됩니까?",
        "샘플사업의 기간은 얼마나 길어?",
        "샘플사업의 기간은 얼마나 걸리나요?",
    ],
)
def test_an_amount_measure_question_strips_its_open_class_predicate(question):
    """`얼마나` complements a conjugated predicate, which is an open class.

    A closed verb list would be a losing game -- `되나요`, `됩니까`, `길어`,
    `걸리나요` are four spellings of the same question -- so the bound is the
    literal `얼마나` plus a capped wildcard rather than an enumeration.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("기간", "기간은")


@pytest.mark.parametrize(
    "question", ["샘플사업의 기간은 얼마나?", "샘플사업의 기간은 얼마나 오래 걸리나요?"]
)
def test_the_amount_predicate_may_be_absent_or_two_words(question):
    """Both ends of the wildcard's word count are load-bearing.

    `얼마나?` carries no predicate at all and `얼마나 오래 걸리나요?` carries
    two words, so a cap of one word in either direction drops one of them.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("기간", "기간은")


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        ("샘플사업의 기간은 얼마나 3개월?", ("기간은 얼마나 3개월",)),
        ("샘플사업의 기간은 얼마나 A?", ("기간은 얼마나 A",)),
        ("샘플사업의 기간은 얼마나 오래-걸리나요?", ("기간은 얼마나 오래-걸리나요",)),
    ],
)
def test_the_amount_wildcard_spans_hangul_only(question, candidates):
    """The wildcard is Hangul syllables, not any non-space run.

    A conjugated Korean predicate is written in Hangul, so a digit, a Latin
    letter or a hyphen means the tail is not the conjugation this rule claims to
    recognise. Widened to `\\S` all three lose everything from `얼마나` rightward
    and ask for `기간` instead of the words the question spelled -- and nothing
    else here notices, because every other amount fixture is pure Hangul.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


@pytest.mark.parametrize(
    ("predicate", "candidates"),
    [
        # One word, where a run may end mid-word, so two runs reach twelve.
        ("소요되겠습니까", ("기간", "기간은")),  # 7
        ("가나다라마바사아자차카타", ("기간", "기간은")),  # 12, the ceiling
        ("가나다라마바사아자차카타파", ("기간은 얼마나 가나다라마바사아자차카타파",)),  # 13
        # Two words: neither run may cross the space, so each word costs one.
        ("가나다라마바 사아자차카타", ("기간", "기간은")),  # 6+6, the ceiling again
        ("가나다 라마바사아자", ("기간", "기간은")),  # 3+6
        ("가나 다라마바사아자", ("기간은 얼마나 가나 다라마바사아자",)),  # 2+7
    ],
)
def test_the_amount_wildcard_caps_each_run_and_not_only_the_total(
    predicate, candidates
):
    """Twelve syllables, in at most two runs of at most six.

    Nothing else pins those numbers -- widening each run from six to twelve, or
    to ten, leaves every other test here passing. The last two rows are what
    makes the per-run half falsifiable: `3+6` is stripped and the shorter `2+7`
    is not, because one run is spent on the first word and six cannot cover the
    second. A cap on the total alone would take both, or neither.
    """
    intent = deterministic_query_intent(f"샘플사업의 기간은 얼마나 {predicate}?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


def test_the_measure_interrogative_must_begin_a_word():
    """`몇몇` is an ordinary Korean determiner, not `몇` plus a stray syllable.

    Without the word-boundary guard the second syllable matched the bare-`몇`
    form and cut the label to `몇` -- the `개요`->`개` hazard, one constant over.
    Only the bare spelling exercises the guard: behind a josa the rule cannot fire
    at all -- the trailing `은` of `몇몇은` is neither a counter nor a
    predicate, so nothing reaches the end anchor -- and `몇몇은?` would pass
    with the guard removed.
    """
    intent = deterministic_query_intent("샘플대상의 몇몇?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("몇몇",)


def test_the_counter_list_stays_closed():
    """The closed counter list is otherwise pinned by nothing.

    Its two siblings mask it: opening the list to any Hangul word takes the
    whole of `몇몇?` and the whole of `몇분야?`, and a label the strip empties is
    read whole, so both still name themselves. Only here does the wildcard leave
    something behind -- it eats `몇몇` and asks for `최근` -- so this is the only
    test that fails when the counter list becomes a wildcard, which is why it
    survives despite `최근 몇몇` being a marginal relation name.
    """
    intent = deterministic_query_intent("샘플대상의 최근 몇몇?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("최근 몇몇",)


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        ("샘플대상의 가격은얼마나?", ("가격은얼마나",)),
        ("샘플사업의 기간은얼마나 길어?", ("기간은얼마나 길어",)),
        ("샘플대상의 비용은얼마나 되나요?", ("비용은얼마나 되나요",)),
    ],
)
def test_the_amount_interrogative_must_begin_a_word_too(question, candidates):
    """The word-boundary guard on `얼마나`, which its sibling does not pin.

    Splitting the guard per interrogative shows that dropping it from `얼마나`
    alone survives every other test here -- `몇몇?` only exercises the `몇` half.
    Without it these labels lose everything from `얼마나` rightward and ask for
    `가격`, `기간`, `비용` instead of the words the question spelled.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


def test_the_measure_tail_matches_only_at_the_end_of_the_label():
    """`몇` inside a label is not a tail.

    Unanchored, the rule ate forward from the `몇` and left `째 심사` -- a
    fragment of the relation the question named.
    """
    intent = deterministic_query_intent("샘플사업의 몇 번째 심사는?")

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == ("몇 번째 심사", "몇 번째 심사는")


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        ("샘플대상의 몇 개?", ("몇 개",)),
        ("샘플대상의 얼마나 많은 인원?", ("얼마나 많은 인원",)),
        ("샘플문서의 몇분야?", ("몇분야",)),
    ],
)
def test_a_label_the_measure_strip_would_empty_is_read_whole(question, candidates):
    """The tail is the whole label here, so the label is read whole instead.

    Declining a whole-label measure question is worse than claiming it: an
    emptied label is declined, which routes the question past
    `_reinterpret_empty_plan` -- the gate that refuses a model `no_answer`,
    the reading Ask renders as `VERIFIED — engine (negative)`. Should the model
    then return an intent that itself plans empty, that plan loses the
    direct-Datalog fallback too. These stay exactly where they are without this
    change.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


@pytest.mark.parametrize(
    ("question", "candidates"),
    [
        ("샘플제품의 이 몇 개인가?", ("이 몇 개",)),
        ("샘플광산의 은 몇 개인가?", ("은 몇 개",)),
        ("샘플대상의 가 몇 개?", ("가 몇 개",)),
        ("샘플모임의 누구 몇 명인가?", ("누구 몇 명",)),
    ],
)
def test_a_label_the_later_strips_would_empty_is_read_whole(question, candidates):
    """Not emptying a label takes more than not letting *this* strip empty it.

    Two more strips run after the measure tail, and what the measure tail leaves
    behind is exactly what they are built to remove: a lone josa syllable, or an
    interrogative. Guarding on the measure strip's own output lets those finish
    the job and decline the question anyway, which is the harm the guard exists
    to prevent. Guarding on the finished readings is what actually holds these
    where they were, so the measure rule adds nothing to the declined class.
    """
    intent = deterministic_query_intent(question)

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.relation_candidates == candidates


@pytest.mark.parametrize(
    "question", ["샘플인물의 나이는 몇 살이야", "샘플사업의 기간은 얼마나 되나요"]
)
def test_a_measure_question_without_a_question_mark_is_still_declined(question):
    """The measure tails are added to the label cleaner, not to the shape check.

    `_looks_like_korean_attribute_question` decides whether the flat attribute
    shape is claimed at all, and only for a question with no `?`. Widening it
    with the measure predicates would claim these, but would also flatten a
    no-`?` multi-hop question the model can read as two hops -- the trade its
    own comment records declining. Nothing here changes that.
    """
    assert (
        deterministic_query_intent(question).kind
        == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
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


# --- the measure-unit caveat (#445) ----------------------------------------
#
# `korean_measure_unit_mismatch` decides whether a verified answer is shown with
# a caveat saying it is stated in a different unit from the one the question
# asked in. Every assertion here is on the exact pair or on None: a mutation that
# returned the family key instead of the spelling would print "the verified value
# states MONTH" to a Korean reader, and only an exact comparison catches that.

_PROSE_VALUES = (
    "샘플부서 연간 계획 수립",
    "연내 완료 예정",
    "주간 보고 체계 운영",
    "일정 조율 진행 중",
    "분기별 실적 검토",
    "시간제 근무 허용",
    "초기 검토 단계",
    "원격 근무 원칙",
    "달성 여부 미정",
    "내년 상반기 착수",
    "지원 대상 아님",
    "분야별 담당 지정",
    "개요 작성 완료",
    "일반 관리 대상",
    "주요 위험 식별",
    "초안 검토 요청",
    "연구 개발 과제",
    "세부 항목 없음",
    "배포 준비 완료",
    "유로존 시장 조사",
    "엔진 점검 필요",
    "달력 기준 산정",
    "세금 계산 제외",
    "살균 처리 완료",
    "프로세스 표준화",
    "퍼센트 표기 통일",
    "제3자 검토 필요",
    "2단계 진행 중",
    "3차 회의 예정",
    "제1분기 마감",
    "5개년 계획 수립",
    "1순위 과제",
    "샘플문서 v2 초안",
    "2년차 담당자 배정",
    "3일간의 일정 확정",
    "3주간격 점검",
    "6월 착수 예정",
    "2021년 기준",
    "second review pending",
    "yearly summary drafted",
    "monthly report archived",
    "weekly sync scheduled",
    "daily standup held",
    "hourly rate undisclosed",
    "minute taking assigned",
    "percentage not disclosed",
    "dollar cost averaging",
    "3 secondary reviews",
    "2 daylight sessions",
)
"""Synthetic values a KB could plausibly hold that state no quantity.

Each one contains a syllable or word that *is* a unit spelling -- `연간`,
`주간`, `일정`, `분기`, `시간제`, `초기`, `원격`, `달성`, `내년`, `지원`,
`분야`, `세금`, `살균`, `프로세스`, `second`, `weekly` -- with no number in
front of it. Fourteen of them do carry a digit, as near-misses: an ordinal
(`제3자`), a stage number (`2단계`), a sequence number (`3차`), a quarter
(`제1분기`), a plan title (`5개년`), a rank (`1순위`), a version (`v2`), a unit
run into the next syllable (`2년차`), two suffixes outside the closed set
(`3일간의`, `3주간격`), a bare month (`6월`), a calendar year (`2021년`), and two
Latin words that begin with a unit spelling (`3 secondary`, `2 daylight`).
"""


def _unit_bearing_counters() -> tuple[str, ...]:
    """The counters a question can ask in that also name a unit.

    Derived from the two tables rather than listed, so the sweep below covers
    every counter that can reach the caveat as the table stands.
    """
    from verinote.pipeline.query_intent import (
        _KOREAN_MEASURE_COUNTER,
        _MEASUREMENT_UNIT_SPELLINGS,
    )

    return tuple(
        counter
        for counter in _KOREAN_MEASURE_COUNTER.split("|")
        if counter in _MEASUREMENT_UNIT_SPELLINGS
    )


def test_a_measure_question_answered_in_another_unit_names_both_spellings():
    """The headline case from #445, at the level that decides it.

    `샘플사업의 기간은 몇 개월인가?` against a KB holding `2년` is answered `2년`
    and verified, and that stays true -- this only supplies the pair the caveat
    beside it is worded from.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년") == (
        "개월",
        "년",
    )


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "6개월"),
        ("샘플사업의 기간은 몇 개월인가?", "2년 6개월"),
        ("샘플사업의 기간은 몇 달인가?", "6개월"),
        ("샘플사업의 비율은 몇 퍼센트인가?", "30%"),
        ("샘플인물의 나이는 몇 살인가?", "30세"),
    ],
)
def test_the_asked_unit_stated_anywhere_in_the_value_suppresses_the_caveat(question, value):
    """A value that does state the asked unit has nothing to caveat.

    `2년 6개월` answers `몇 개월인가?` in months among other things, so the
    earlier `년` must not fire.

    The last three pin THIS suppression and nothing else. Their asked counter is
    a table row (`달`, `퍼센트`, `살`) whose value-side spelling is a different
    row of the same canonical unit, so deleting the row under test silences the
    *question* side too and the case stays green -- it cannot pin the row. The
    rows are pinned one-sidedly by the test below.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 년인가?", "6달", ("년", "달")),
        ("샘플사업의 증가율은 몇 배인가?", "30%", ("배", "%")),
        ("샘플인물의 나이는 몇 살인가?", "24개월", ("살", "개월")),
    ],
)
def test_a_spellings_row_is_pinned_by_a_question_asked_in_a_different_row(
    question, value, expected
):
    """One row of `_MEASUREMENT_UNIT_SPELLINGS` per case, from one side only.

    The table serves both the question and the value, so a case whose asked
    counter is the row under test cannot pin it. Each case here asks in one row
    and is answered in the row being pinned: delete `달` and `6달` states no unit
    at all, delete `%` and `30%` states none, delete `살` and the question names
    no unit. Every one of the three goes silent, and none of them is masked by
    the same-unit suppression.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플사업의 항목은 몇 개인가?", "2년"),
        ("샘플사업의 기간은 얼마나 되나요?", "2년"),
    ],
)
def test_a_tail_naming_no_unit_is_silent(question, value):
    """`몇 개` and `얼마나` reach the same answer by different routes.

    `개` is a counter `_KOREAN_MEASURE_COUNTER` lists and
    `_MEASUREMENT_UNIT_SPELLINGS` does not, so no unit is derived from it and the
    family table is never consulted. The `얼마나` branch captures no counter at
    all, so the lookup is handed None. Both land on the same `.get` returning
    None rather than on a branch apiece.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_a_relation_literally_named_with_a_counter_is_not_a_measure_question():
    """`샘플사업의 몇 년인가?` asks *for* `몇 년`, not *in* years.

    The measure tail is the whole label, so nothing is left in front of it to
    read as a relation -- and `_korean_attribute_label_readings` correspondingly
    reads the label whole and asks the KB for a relation named `몇 년`. A KB that
    holds one answers it, and telling that reader the value is in the wrong unit
    would be nonsense.
    """
    from verinote.pipeline.query_intent import (
        _korean_attribute_label_readings,
        korean_measure_unit_mismatch,
    )

    assert _korean_attribute_label_readings("몇 년") == ("몇 년",)
    assert korean_measure_unit_mismatch("샘플사업의 몇 년인가?", "24개월") is None


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플인물의 역할은 무엇인가?", "2년"),
        ("샘플기간의 최근 몇 년인가?", "재검토"),
        ("샘플행사의 참석자는 몇 분인가?", "5"),
        ("샘플사업의 주기는 몇 시간인가?", "일 단위 관리"),
        ("샘플사업의 가격은 몇 원인가?", "지원 없음"),
    ],
)
def test_a_question_or_value_with_nothing_to_compare_is_silent(question, value):
    """Nothing to compare on one side or the other.

    `역할은 무엇인가?` has no measure tail. The other four have one and a value
    that states no quantity.

    `참석자는 몇 분인가?` answered `5` is a labelled contract regression rather
    than a pinned mechanism: the value engages no value-side machinery at all, so
    no mutation of this rule can make it fire, and it is kept to record that a
    bare number is never caveated.

    `지원 없음` is likewise carried by the sweep rather than by itself: the bare
    `원` inside `지원` is the unit the question asked in, so the same-unit
    suppression would silence it even with the digit requirement removed. The
    one-sided case for that requirement is in the test below.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_the_digit_requirement_keeps_ordinary_prose_out_of_the_caveat(monkeypatch):
    """Every unit-bearing counter against a corpus of prose values, all silent.

    This is what the leading `[0-9]` in `_VALUE_MEASUREMENT` buys, and the sweep
    rather than any single case is what measures it.

    The mutant count is BUILT AND MEASURED here rather than quoted in prose. It
    was quoted once, as 55, and went stale the moment the suppression scan got
    its own pattern: with one pattern serving both halves, mutating
    `_VALUE_MEASUREMENT` mutated suppression too, and 13 of the fires that
    number counted are now suppressed by `_VALUE_MEASUREMENT_RELAXED` instead.
    A figure in a docstring cannot notice that; this assertion can.

    A one-sided case for the requirement, not masked by the same-unit
    suppression: `몇 개월인가?` answered `내년 착수` is silent as written and
    fires `(개월, 년)` without the digit prefix.
    """
    import re

    import verinote.pipeline.query_intent as query_intent
    from verinote.pipeline.query_intent import (
        _VALUE_MEASUREMENT,
        korean_measure_unit_mismatch,
    )

    counters = _unit_bearing_counters()
    assert len(counters) == 13
    questions = [f"샘플대상의 지표는 몇 {counter}인가?" for counter in counters]
    pairs = [(question, value) for question in questions for value in _PROSE_VALUES]
    assert len(pairs) == len(counters) * len(_PROSE_VALUES)

    for question, value in pairs:
        assert korean_measure_unit_mismatch(question, value) is None, (question, value)

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "내년 착수") is None

    # The mutation the sentence above names: the reporting pattern only, with
    # the suppression pattern left alone. Mutating both instead gives 55, which
    # is what the stale figure was measuring.
    #
    # Sliced off the live pattern rather than rebuilt from parts, so the mutant
    # is this pattern minus its digit head and nothing else. A hand-built copy
    # would drift from the source silently, and asserting text equality against
    # one would fail whenever the alternation is legitimately reordered -- the
    # `startswith` is the tie, and it is the only thing this hardcodes.
    digit_head = r"[0-9][0-9,.]*"
    assert _VALUE_MEASUREMENT.pattern.startswith(digit_head)
    without_digits = re.compile(_VALUE_MEASUREMENT.pattern[len(digit_head):])
    monkeypatch.setattr(query_intent, "_VALUE_MEASUREMENT", without_digits)
    fires = sum(
        1 for question, value in pairs
        if korean_measure_unit_mismatch(question, value) is not None
    )
    assert fires == 68


def test_a_cross_family_unit_is_not_a_unit_mismatch():
    """`몇 원인가?` answered `2년` is not a money value in the wrong currency.

    Money and time are not convertible, so "no unit conversion is applied" would
    be the wrong thing to say about it. Within a family the caveat is right:
    `1000달러` answering `몇 원인가?` is the same quantity in another currency.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "2년") is None
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "1000달러") == (
        "원",
        "달러",
    )


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "30% 완료, 3주", ("개월", "주")),
        ("샘플사업의 가격은 몇 원인가?", "30% 할인, 3달러", ("원", "달러")),
        ("샘플사업의 기간은 몇 개월인가?", "2배 증가, 3주 소요", ("개월", "주")),
    ],
)
def test_the_first_same_family_unit_is_reported_not_the_first_unit(
    question, value, expected
):
    """An earlier cross-family quantity does not silence a later same-family one.

    Each value states a ratio first and the quantity the question was about
    second. Reporting the first unit found would name a percentage or a multiple
    beside a question about months or won, and reverting the selection to the
    first unit makes all three go silent instead, because the ratio is in the
    wrong family.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


def test_a_calendar_date_silences_the_whole_value():
    """A date is a point in time, not a quantity of it -- at the cost named here.

    `2021년` must not be reported as stating years. The guard is on the whole
    value rather than on the matched span, so a value containing a calendar date
    reports no units at all, including a genuine mismatch stated elsewhere in the
    same value. Both costs below are real and accepted.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2021년") is None
    assert (
        korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2021년 착수, 총 3주")
        is None
    )
    assert (
        korean_measure_unit_mismatch("샘플회의의 시간은 몇 시간인가?", "2021년 기준 30분")
        is None
    )


def test_a_longer_digit_run_is_not_read_as_a_calendar_year():
    """The left-hand `(?<![0-9])`: without it, `10000년` matches on `0000년`.

    A genuine ten-thousand-year value would then be read as a calendar date and
    silenced. The right-hand bound is pinned separately below. `1500년` and
    `2000년간` stay on the date side; both spellings are really used for
    durations too, so that reading is ambiguous and this rule picks the date one.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "10000년") == (
        "개월",
        "년",
    )
    assert _value_measure_units("1500년") == ()
    assert _value_measure_units("2000년간") == ()


@pytest.mark.parametrize(
    "value",
    ["2년차", "3일간의 일정", "3주간격", "제1분기", "3 secondary reviews"],
)
def test_a_unit_continued_by_another_letter_is_not_read(value):
    """The trailing lookahead: a unit run into Hangul, a digit, or Latin is not one.

    `2년차` is a second year of service, `3일간의 일정` and `3주간격` carry
    suffixes outside the closed set, `제1분기` is a quarter, and
    `3 secondary reviews` begins with `second` and continues in Latin. Each is
    asked in a *different* unit of the same family, so none of them is masked by
    the same-unit suppression -- `몇 초인가?` would have masked the last one.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


def test_a_suffix_inside_the_closed_set_still_reads_the_unit():
    """`2년간` states two years; the suffix does not hide the unit."""
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년간") == (
        "개월",
        "년",
    )


def test_a_longer_spelling_is_reached_by_backtracking_past_a_shorter_one():
    """`주일` and `달러` are read even though `주` and `달` are tried first.

    The alternation is in table order, so `주` and `달` match first and are then
    rejected by the lookahead, which sees the Hangul that follows them; the
    engine backtracks into the longer row. That mechanism, not the ordering, is
    what makes these read -- every prefix pair in the table today is extended by
    a Hangul or Latin character, both inside the lookahead's class.
    """
    from verinote.pipeline.query_intent import _value_measure_units

    assert _value_measure_units("2주일") == (("WEEK", "주일"),)
    assert _value_measure_units("3달러") == (("USD", "달러"),)


def test_bare_월_is_a_month_of_the_year_and_is_not_read():
    """`3월` is March and `6월` is June, so neither states a month-count.

    `개월` is the counter for a month-count and is in the table; bare `월` is
    deliberately not. `일` is in the table because `3일` is three days far more
    often than it is the third of the month, and a day-of-month arrives with its
    month beside it, which `_CALENDAR_DATE` catches.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 일인가?", "3월 착수") is None
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "6월") is None
    assert _value_measure_units("3일") == (("DAY", "일"),)


def test_a_prefix_currency_symbol_is_out_of_reach_by_construction():
    """`$`, `₩` and `€` precede their number, and every quantity starts at a digit.

    Their absence from the table is therefore not a row that could be restored:
    no row spelled that way would ever be reached. `%` is read because it follows
    the number.
    """
    from verinote.pipeline.query_intent import _value_measure_units

    assert _value_measure_units("$1000") == ()
    assert _value_measure_units("₩1000") == ()
    assert _value_measure_units("€1000") == ()
    assert _value_measure_units("30%") == (("PERCENT", "%"),)


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "30세", ("개월", "세")),
        ("샘플사업의 증가율은 몇 배인가?", "120퍼센트", ("배", "퍼센트")),
    ],
)
def test_further_rows_report_the_spelling_the_value_used(question, value, expected):
    """The stated spelling is reported, never the canonical unit.

    `30세` states `세`, not `YEAR`, and `120퍼센트` states `퍼센트`, not
    `PERCENT`. A caveat naming the family key would print an English identifier
    to a Korean reader.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


def test_a_value_written_in_nfd_states_its_unit():
    """The value is composed before it is read.

    `Store.add_fact` keeps the `object` column as written, so a decomposed `2년`
    reaches this rule as four code points while the alternation is composed. Drop
    the `nfc` call and this value states nothing.
    """
    import unicodedata

    from verinote.pipeline.query_intent import _value_measure_units

    decomposed = unicodedata.normalize("NFD", "2년")
    assert len(decomposed) == 4
    assert _value_measure_units(decomposed) == (("YEAR", "년"),)


_ROW_FIXTURES = {
    "년": "2년", "연": "3연", "살": "30살", "세": "30세",
    "year": "1 year", "years": "2 years",
    "개월": "6개월", "달": "6달", "month": "1 month", "months": "2 months",
    "주": "3주", "주일": "2주일", "week": "1 week", "weeks": "2 weeks",
    "일": "3일", "day": "1 day", "days": "2 days",
    "시간": "5시간", "hour": "1 hour", "hours": "2 hours",
    "분": "30분", "minute": "1 minute", "minutes": "2 minutes",
    "초": "10초", "second": "1 second", "seconds": "2 seconds",
    "퍼센트": "30퍼센트", "프로": "30프로", "%": "30%", "percent": "30 percent",
    "배": "2배",
    "원": "1000원", "won": "1000 won",
    "달러": "3달러", "dollar": "1 dollar", "dollars": "2 dollars",
    "엔": "1000엔", "yen": "1000 yen",
    "유로": "500유로", "euro": "500 euro",
}
"""One value per row of `_MEASUREMENT_UNIT_SPELLINGS`, stating that row's unit.

`_value_measure_units` reads only the value, so a fixture here is one-sided by
construction: deleting the row it exercises leaves its value stating nothing,
whatever the question. That is what the mismatch-level cases cannot do for a row
whose only counter is the row itself -- `won` has no second money counter to ask
in, since `원` is the only money spelling `_KOREAN_MEASURE_COUNTER` lists.

`3연` is the one contrived value: `연` means a year but is not used as a counter
after a number in ordinary Korean, so this pins the row's reachability rather
than a spelling a KB would hold.
"""


def test_every_spellings_row_has_a_value_that_states_it():
    """Each row read back one-sidedly, and no row left without a fixture.

    The equality on `_ROW_FIXTURES`' key set is the half that keeps this honest:
    a row added to the table with no value exercising it fails here rather than
    joining silently.
    """
    from verinote.pipeline.query_intent import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _value_measure_units,
    )

    assert set(_ROW_FIXTURES) == set(_MEASUREMENT_UNIT_SPELLINGS)
    for spelling, value in _ROW_FIXTURES.items():
        assert _value_measure_units(value) == (
            (_MEASUREMENT_UNIT_SPELLINGS[spelling], spelling),
        ), (spelling, value)


def test_a_korean_magnitude_word_between_the_digits_and_the_unit_is_read():
    """`3만 달러` is thirty thousand dollars, and the `만` must not break the read.

    Without `[만억천조]?` in `_VALUE_MEASUREMENT` the digits no longer reach the
    unit and the value states nothing.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3만 달러") == (("USD", "달러"),)
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "3만 달러") == (
        "원",
        "달러",
    )


def test_a_latin_spelling_is_matched_and_reported_casefolded():
    """The value is casefolded before it is read, and that shows in the caveat.

    `3 Weeks` states weeks, and the sentence beside the answer will say the value
    states `weeks` rather than `Weeks`. Reporting the folded spelling is the
    accepted cost of matching a table written in lower case.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3 Weeks") == (("WEEK", "weeks"),)
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "3 Weeks") == (
        "개월",
        "weeks",
    )


def test_a_four_digit_year_run_into_more_digits_is_not_a_calendar_year():
    """The right-hand `(?![0-9])` of the year branch, pinned on its own.

    The left-hand `(?<![0-9])` is what `10000년` needs; this is the other half,
    and the value that separates them is contrived: `2021년12개월` is not a
    spelling a KB would plausibly hold, but without the right-hand bound it is
    read as a calendar year and its genuine `12개월` is silenced with it.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    # This first assertion is the pin: without the right-hand bound the value is
    # a calendar date and states nothing at all.
    assert _value_measure_units("2021년12개월") == (("MONTH", "개월"),)
    # Asked in weeks rather than years, because the value does carry a quantity
    # in years -- `2021년` -- and `_value_states_asked_unit` correctly suppresses
    # a years question on it however the year branch is bounded.
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 주인가?", "2021년12개월") == (
        "주",
        "개월",
    )


@pytest.mark.parametrize(
    ("value", "branch", "unit_it_would_wrongly_state"),
    [
        ("3월 15일 착수", "digit month and day", "일"),
        ("21년 3월 ~ 22년 2월", "year and month", "년"),
        ("2021년 착수, 총 3주", "four-digit year", "년"),
        ("2021.03.15 일", "ISO-style date", "일"),
    ],
)
def test_each_calendar_date_branch_is_needed_by_one_of_these_values(
    value, branch, unit_it_would_wrongly_state
):
    """One value per alternative of `_CALENDAR_DATE`, so no branch rides free.

    A guard with four alternatives needs four killers. Pinning only the
    four-digit-year branch left the other three deletable with the suite green,
    which is the same defect as a spellings row with no fixture: one covered
    alternative makes the whole guard look covered.

    Deleting the branch each value exercises makes exactly that value fire, and
    only that value -- the deletion matrix over the four branches is a clean
    diagonal, so none is masking another. The last parameter records what the
    caveat would then wrongly say the value states.

    The ISO value is the one that justifies its branch rather than merely
    exercising it. A bare `2021-03-15` states no unit, so the branch does nothing
    for it; what the branch is for is a unit spelling run onto the end of a date,
    which is read as days without it. That branch also costs real caveats
    (`2024/01/02 3주` states a genuine duration and is silenced), and it is kept
    anyway because a wrong sentence is worse than a missing one.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플계약의 기간은 몇 개월인가?", value) is None


def test_a_full_width_number_states_no_quantity():
    """`[0-9]` is ASCII, and `nfc` is not `nfkc`, so `３년` states nothing.

    The silence is specifically the digits: a non-breaking space between the
    number and the unit is folded by nothing and needs no folding, because
    `\\s` already admits it.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("３년") == ()
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "３년") is None
    # Written as the escape rather than pasted, so the non-breaking space
    # cannot be mistaken for -- or reflowed into -- an ordinary one.
    assert _value_measure_units("3\xa0년") == (("YEAR", "년"),)


@pytest.mark.parametrize(
    ("question", "value", "wrong_output", "what_the_value_really_says"),
    [
        ("샘플계약의 정산일은 몇 개월인가?", "매월 15일", ("개월", "일"), "the 15th of each month"),
        ("샘플계약의 마감일은 몇 개월인가?", "15일 마감", ("개월", "일"), "a deadline on the 15th"),
        ("샘플회의의 시간은 몇 시간인가?", "3시 30분", ("시간", "분"), "half past three"),
        ("샘플회의의 시간은 몇 시간인가?", "오후 2시 15분", ("시간", "분"), "a quarter past two"),
        ("샘플계약의 기간은 몇 개월인가?", "3월 중 15일", ("개월", "일"), "the 15th, within March"),
        ("샘플계약의 기간은 몇 개월인가?", "3월의 15일", ("개월", "일"), "the 15th of March"),
        ("샘플계약의 기간은 몇 개월인가?", "3월 말 15일", ("개월", "일"), "the 15th, late March"),
        ("샘플계약의 기간은 몇 개월인가?", "03/15일", ("개월", "일"), "March 15th, no year"),
        ("샘플사업의 가격은 몇 원인가?", "2천만원 (15,000달러)", ("원", "달러"), "20 million won"),
        ("샘플사업의 가격은 몇 원인가?", "1억5천만원 및 20,000달러", ("원", "달러"), "150 million won"),
        ("샘플사업의 기간은 몇 년인가?", "5개년 계획 3주", ("년", "주"), "a five-year plan"),
        ("샘플사업의 기간은 몇 년인가?", "３년 30주", ("년", "주"), "three years, full-width"),
        ("샘플사업의 기간은 몇 개월인가?", "6월 및 30주", ("개월", "주"), "June, or six months"),
        ("샘플사업의 기간은 몇 개월인가?", "21년", ("개월", "년"), "the year 2021, or 21 years"),
        ("샘플사업의 소요는 몇 분인가?", "2 second review", ("분", "second"), "a second review"),
        ("샘플사업의 기간은 몇 년인가?", "100주", ("년", "주"), "one hundred shares"),
        ("샘플회의의 시간은 몇 시간인가?", "5분", ("시간", "분"), "five people, honorific"),
    ],
)
def test_known_false_unit_statements_are_recorded_not_fixed(
    question, value, wrong_output, what_the_value_really_says
):
    """The caveat is wrong on these, and this records it rather than hiding it.

    These are the wrong sentences that have been found, not all the ones that
    exist -- every round of review on #445 has added to the list, and each
    addition was a case an earlier round's reasoning had ruled out. So this is a
    record, not a boundary, and it deliberately claims no shared cause: the
    two-digit year is not a lexical ambiguity and `second` is not Korean, so any
    argument of the form "the known ones are all X, therefore a non-X input is
    safe" is unsound on its face.

    What is true of the individual entries. Day-of-month and time-of-day are
    points in time, not quantities of time: `_CALENDAR_DATE` reaches a day of the
    month only through a digit month, so `매월 15일` is not a date to it, and the
    guard has no time-of-day branch at all -- `시` is outside the spellings table
    while `분` is in it. Neither needs a strained question; `시간` asked in
    `몇 시간` is the most on-point relation there is. `21년` is left reading YEAR
    because twenty-one years is a real duration -- the year+month branch takes
    `21년 3월`, and nothing here separates the bare form. `2 second review` is an
    English ordinal sitting on a unit row that pays for itself elsewhere
    (`30 seconds` asked in `몇 분`). Only `100주` and `5분` need a question asked
    in a unit the relation does not really measure.

    None of this is fixed here; #445 asks that a verified answer in another unit
    be caveated, not that every ambiguity be resolved. Asserting the wrong output
    is deliberate: if a later change fixes one of these, this test fails and the
    disclosure in `korean_measure_unit_mismatch` has to be corrected with it,
    instead of quietly going stale.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == wrong_output


@pytest.mark.parametrize(
    "value",
    ["21년 3월 ~ 22년 2월", "25년 12월 착수", "12년 6월", "2021년 3월"],
)
def test_a_year_followed_by_a_month_is_a_date_at_any_year_width(value):
    """Two-digit years are ordinary Korean notation, and were read as durations.

    `기간` asked in `몇 개월` is the issue's own headline question, so this needed
    no strained relation: a `기간` holding `21년 3월 ~ 22년 2월` was told "the
    verified value states 년", of a value that names no duration at all. The
    year+month branch reads it as the date it is.

    Delete that branch and every value here fires `(개월, 년)`.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("21년", ("개월", "년")),
        ("21년 계약", ("개월", "년")),
        ("10000년", ("개월", "년")),
    ],
)
def test_a_year_with_no_month_beside_it_still_reads_as_a_duration(value, expected):
    """The other half of the year+month branch: what it deliberately does NOT take.

    Widening the four-digit year branch to `[0-9]{2,4}` would silence all of
    these, and nothing in the diff used to notice. `21년` on its own really can
    be twenty-one years, so it keeps reading YEAR and is disclosed as a wrong
    sentence when it is not; that is the trade, and this is the assertion that
    makes reversing it fail rather than pass.

    `10000년` is here for a second reason: it also pins that the year+month
    branch did not acquire a way to match a five-digit run.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) == expected


@pytest.mark.parametrize("value", ["12년 6개월", "2년 3개월", "1년 6개월"])
def test_a_duration_written_with_개월_is_not_swallowed_by_the_year_month_branch(value):
    """The counter is what separates a duration from a date, not the number.

    `12년 6개월` is twelve years and six months and has the same digits as a date
    would; what makes it a duration is `개월` rather than bare `월`. All three
    state months, so the same-unit suppression is what silences them -- if the
    year+month branch had swallowed them they would be silent for the wrong
    reason, which the unit list below the assertion distinguishes.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None
    assert ("MONTH", "개월") in _value_measure_units(value)


def test_the_iso_branch_silences_true_caveats_as_well_as_false_ones():
    """The ISO branch's cost, recorded beside the case that justifies it.

    `2021.03.15 일` is a date with a unit spelling on the end, and without the
    branch it is reported as stating days -- that is what the branch is for. But
    the guard is whole-value, so the same branch silences a genuine duration
    stated next to a date. Both values below state a real mismatch with a
    question asked in months, and both are silent.

    Recorded rather than fixed. Taking the first without the second needs a
    span-local guard, which is a change of its own; keeping the branch follows
    this rule's standing preference for a missing sentence over a wrong one.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    question = "샘플계약의 기간은 몇 개월인가?"
    assert korean_measure_unit_mismatch(question, "2024/01/02 3주") is None
    assert korean_measure_unit_mismatch(question, "2021-03-15 (3일)") is None
    # The durations really are there; it is the guard that hides them.
    assert _value_measure_units("3주") == (("WEEK", "주"),)
    assert _value_measure_units("(3일)") == (("DAY", "일"),)


def test_a_single_digit_year_before_a_month_is_left_alone():
    """The year+month branch's lower bound, which is a judgement, not a fact.

    `[0-9]{2,4}` takes `21년 3월` and leaves `2년 3월`. Both readings of the
    latter are rare -- a duration would normally be written `2년 3개월`, and a
    date `2년 3월` only makes sense in a relative or fiscal year -- so neither
    side is clearly right and the rule takes the conservative one, changing as
    little as possible. This records which side that is, so widening the bound
    to `[0-9]{1,4}` fails here rather than passing quietly.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년 3월") == (
        "개월",
        "년",
    )


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플작업의 소요시간은 몇 시간인가?", "3시간30분"),
        ("샘플작업의 소요시간은 몇 시간인가?", "3시간 30분"),
        ("샘플사업의 기간은 몇 년인가?", "2년6개월"),
        ("샘플사업의 기간은 몇 년인가?", "2년 6개월"),
        ("샘플사업의 기간은 몇 주인가?", "1주2일"),
        ("샘플사업의 기간은 몇 개월인가?", "2년 6개월의 사업기간"),
        ("샘플사업의 기간은 몇 개월인가?", "2개월10일"),
    ],
)
def test_a_quantity_in_the_asked_unit_suppresses_the_caveat_however_it_is_spaced(
    question, value
):
    """Spacing must not decide whether a correct answer gets a wrong caveat.

    `_VALUE_MEASUREMENT`'s trailing lookahead refuses a unit run into the next
    character, which is right for reading what a value states and wrong for
    asking whether the value already carries the unit the question wanted. It
    hid the asked unit in every unspaced case here, and the caveat fired: a
    reader asking `몇 시간인가?` of `3시간30분` was told the value states minutes
    and that verinote applies no conversion, when the leading quantity is
    exactly the hours asked for. `3시간 30분`, one space apart, was silent.

    The spaced and unspaced forms are paired deliberately, so a regression that
    reintroduces the asymmetry fails on the pair rather than on a single case.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_the_suppression_scan_does_not_read_a_shorter_spelling_inside_a_longer():
    """Why the suppression scan is longest-first rather than lookahead-free.

    Dropping the lookahead is what lets `3시간30분` suppress, but dropping it
    without reordering would let a months question find `달` inside `달러` and
    suppress a caveat the value never earned. The alternation is sorted longest
    spelling first, so `달러` is taken before `달`.

    The second assertion is the one that would fail under a bare substring test
    or a per-unit scan with no ordering: the value states dollars and weeks, the
    question asked months, and the weeks caveat must survive.
    """
    from verinote.pipeline.query_intent import (
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("3달러", "MONTH") is False
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "3달러, 2주 소요") == (
        "개월",
        "주",
    )
    assert _value_states_asked_unit("2주일", "WEEK") is True
    assert _value_states_asked_unit("3시간30분", "HOUR") is True


def test_the_suppression_scan_sees_everything_the_value_scan_sees():
    """The relaxed scan is a superset, so it subsumes the equality test it replaced.

    If some unit could be read by `_value_measure_units` and missed by
    `_value_states_asked_unit`, a value stating the asked unit plainly would
    stop suppressing and the caveat would fire on it. Swept over every spelling
    against a set of following characters that exercise the lookahead.
    """
    from verinote.pipeline.query_intent import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _value_measure_units,
        _value_states_asked_unit,
    )

    # The number set carries a magnitude word deliberately. Without one, the
    # relaxed pattern could lose `[만억천조]?` and this property would still
    # hold, which is how `3만 원, 5달러` reached `('원', '원')` unnoticed.
    values = [
        f"{number}{spelling}{tail}"
        for number in ("1", "3", "1000", "3만", "1억", "2천")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        for tail in ("", " ", "차", "5", "의", "간", "러", "s")
    ]
    for value in values:
        for unit, _ in _value_measure_units(value):
            assert _value_states_asked_unit(value, unit), (value, unit)


@pytest.mark.parametrize("value", ["21.03.15일", "25-01-15일", "2021.03.15 일"])
def test_the_iso_branch_reads_a_two_digit_year_like_every_other_branch(value):
    """The year+month branch takes two-digit years; the ISO branch now does too.

    Holding this branch at four digits while the branch above it accepted two --
    on the stated grounds that two-digit years are ordinary Korean notation --
    was the same premise accepted in one place and refused in the next, and
    `21.03.15일` was read as fifteen days because of it.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


@pytest.mark.parametrize("value", ["2.03.15일", "12021.03.15일"])
def test_the_iso_year_is_bounded_below_and_on_the_left(value):
    """Both new bounds on the ISO year, pinned the way branch 2's are.

    `2.03.15일` has a one-digit year and `12021.03.15일` a five-digit run; widen
    to `{1,4}` or drop the `(?<![0-9])` and each becomes a date, silencing a
    value this rule otherwise reads. Both are contrived -- that is the point of
    a bound -- but an unpinned bound is one a later change removes for free.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) == (
        "개월",
        "일",
    )


@pytest.mark.parametrize("suffix", ["간", "가량", "정도", "쯤", "짜리"])
def test_every_unit_suffix_alternative_has_a_value_that_needs_it(suffix):
    """One value per member of `_UNIT_SUFFIX`, so no member rides free.

    Four of the five were unpinned: dropping `가량`, `정도`, `쯤` or `짜리` left
    the suite green while changing what values read. Same argument as
    `_ROW_FIXTURES` two tables over -- an unpinned entry licences a silent
    deletion -- and the same remedy.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 년인가?", f"3개월{suffix}") == (
        "년",
        "개월",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1000엔", ("원", "엔")), ("1000 yen", ("원", "yen")),
     ("500유로", ("원", "유로")), ("500 euro", ("원", "euro"))],
)
def test_the_yen_and_euro_family_rows_are_pinned(value, expected):
    """`_MEASUREMENT_FAMILY`'s JPY and EUR rows, the two that were unpinned.

    Eleven of the thirteen rows were already killed by existing fixtures; these
    two were reachable only through `_ROW_FIXTURES`, which checks
    `_value_measure_units` and never consults the family table. Remap either to
    another family and the cross-currency caveat here goes silent.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", value) == expected


def test_the_year_month_branch_is_bounded_above_and_on_the_left():
    """Branch 2's `{2,4}` upper bound and its `(?<![0-9])`, both pinned at once.

    `10000년 3월` is a five-digit run followed by a month. Widen the branch to
    `{2,}` and it matches outright; drop the left-hand lookbehind and it matches
    on the inner `0000년 3월`. Either way the value becomes a date and stops
    being read, so one value covers both bounds.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "10000년 3월") == (
        "개월",
        "년",
    )


def test_the_suppression_scan_reads_only_one_magnitude_word():
    """`2천만원` is invisible to both scans, and that is what turns it wrong.

    `[만억천조]?` is one character rather than a run, so a number stacking
    magnitudes states nothing either pattern can see. On its own that is a
    silence. Beside a unit the reporting scan CAN read it becomes a wrong
    sentence: the caveat names the dollars and the reader's answer leads with
    won.

    `1억원` is the control -- one magnitude word, read by both scans, suppressed.
    Recorded, not fixed: widening to `[만억천조]*` is a coverage change with its
    own sweep to run, and #445 asks for the caveat rather than for every Korean
    number format.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("2천만원") == ()
    assert _value_states_asked_unit("2천만원", "KRW") is False
    # One magnitude word is read by both, so this one suppresses.
    assert _value_measure_units("1억원") == (("KRW", "원"),)
    assert _value_states_asked_unit("1억원", "KRW") is True
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "1억원 (15,000달러)") is None


@pytest.mark.parametrize("value", ["3월 15일", "3월  15일", "3월\t15일", "3월15일"])
def test_whitespace_between_a_digit_month_and_its_day_still_reads_as_a_date(value):
    """Branch 1 is `\\s*` on both sides, so whitespace does not separate them.

    The bullet describing this said "immediately beside" while its own example
    carried a space. What ends the match is a word between the two, not a gap:
    `3월 중 15일` is not a date and is covered in the residue test above.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플계약의 기간은 몇 개월인가?", value) is None


def test_the_suppression_scan_keeps_the_digit_head_of_the_strict_pattern():
    """The relaxed pattern's twin of "the whole precision of this rule".

    `_VALUE_MEASUREMENT`'s leading `[0-9]` is pinned by a 637-pair prose sweep,
    and the relaxed pattern was built with the same head and nothing guarding
    it. Drop it there and the bare `원` inside `지원` counts as a quantity in
    won, so this value stops warning about its dollars -- the same prose-noise
    hazard, arriving through the suppression side instead.
    """
    from verinote.pipeline.query_intent import (
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("지원 없음, 3달러", "KRW") is False
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "지원 없음, 3달러") == (
        "원",
        "달러",
    )


def test_the_suppression_scan_reads_the_same_magnitude_word_as_the_value_scan():
    """The two scans must agree on what a quantity looks like, or the caveat lies.

    Drop `[만억천조]?` from the relaxed pattern only, and the two disagree about
    `3만 원`: the reporting scan reads won, the suppression scan does not, so
    nothing suppresses and the first same-family unit reported is the won
    itself. The caveat then renders as "the question's counter is 원; the
    verified value states 원" -- a sentence that contradicts itself in front of
    the reader.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3만 원, 5달러") == (("KRW", "원"), ("USD", "달러"))
    assert _value_states_asked_unit("3만 원, 5달러", "KRW") is True
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "3만 원, 5달러") is None


@pytest.mark.parametrize(
    ("question", "value", "unit_the_caveat_used_to_name"),
    [
        ("샘플사업의 소요는 몇 분인가?", "3분기 실적, 2시간 소요", "시간"),
        ("샘플사업의 기간은 몇 주인가?", "1주년 기념, 3개월 준비", "개월"),
        ("샘플사업의 기간은 몇 년인가?", "80년대 후반, 3개월", "개월"),
        ("샘플사업의 소요는 몇 초인가?", "3 secondary reviews, 2 minutes", "minutes"),
    ],
)
def test_caveats_lost_to_the_suppression_scan_are_recorded_not_fixed(
    question, value, unit_the_caveat_used_to_name
):
    """What the generous suppression reading costs, priced rather than implied.

    A unit spelling that is only a syllable of an unrelated word still counts as
    a quantity in that unit: `3분기` reads MINUTE, `1주년` WEEK, `80년대` YEAR,
    `3 secondary` SECOND. Each value here states a real same-family mismatch
    somewhere else and each is silent because of it. The last parameter records
    the caveat that used to be shown.

    Asserting the silence is deliberate, the same instrument as the wrong-
    sentence record: narrow the suppression scan later and these fail, forcing
    the paragraph in `_VALUE_MEASUREMENT_RELAXED` to be corrected with the code.
    The trade is accepted because the alternative was a wrong sentence on
    `3시간30분`, not because the cost is small.
    """
    from verinote.pipeline.query_intent import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch(question, value) is None
    # The mismatch really is in the value; only the suppression scan hides it.
    assert unit_the_caveat_used_to_name in {
        spelling for _, spelling in _value_measure_units(value)
    }


def test_only_the_달러_before_달_constraint_decides_the_suppression_ordering():
    """The ordering requirement is one prefix pair, not the length rule.

    `달`/`달러` is the only prefix pair in the table that crosses canonical
    units, so it is the only one whose order can change an answer. Longest-first
    is what ships because it states the intent, but it is not privileged.

    Pinned in both directions, and derived from the table rather than hardcoded
    so that reordering `_MEASUREMENT_UNIT_SPELLINGS` cannot make this fail for a
    reason it is not about: every candidate ordering that puts `달러` first reads
    exactly as the shipped pattern does, and every one that does not reads
    differently. The two non-empty assertions keep it from passing vacuously if
    some future table put every candidate on one side.
    """
    import re

    from verinote.pipeline.query_intent import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _VALUE_MEASUREMENT_RELAXED,
    )

    spellings = list(_MEASUREMENT_UNIT_SPELLINGS)
    candidates = {
        "longest-first": sorted(spellings, key=len, reverse=True),
        "shortest-first": sorted(spellings, key=len),
        "table": list(spellings),
        "reversed-table": list(reversed(spellings)),
        "alphabetical": sorted(spellings),
        "reverse-alphabetical": sorted(spellings, reverse=True),
    }
    probes = ["3달러", "2주일", "3만 원", "1000달러", "3달", "2주", "30 seconds", "5달러 및 3달"]

    def reads(pattern):
        return [[m.group("unit") for m in pattern.finditer(v)] for v in probes]

    def compiled(order):
        return re.compile(
            r"[0-9][0-9,.]*\s*[만억천조]?\s*(?P<unit>"
            + "|".join(re.escape(s) for s in order)
            + r")"
        )

    shipped = reads(_VALUE_MEASUREMENT_RELAXED)
    satisfying = {n: o for n, o in candidates.items() if o.index("달러") < o.index("달")}
    violating = {n: o for n, o in candidates.items() if o.index("달러") > o.index("달")}
    assert satisfying, candidates
    assert violating, candidates
    for name, order in satisfying.items():
        assert reads(compiled(order)) == shipped, name
    for name, order in violating.items():
        assert reads(compiled(order)) != shipped, name


@pytest.mark.parametrize(
    "value",
    ["12.5.3 버전, 3주 소요", "10.1.2 릴리스, 3주", "10.0.0.1 서버, 3주"],
)
def test_the_widened_iso_year_also_swallows_version_triples(value):
    """The third cost of the ISO widening, recorded beside the other two.

    Widening the ISO year to two digits made dotted numeric triples look like
    dates, so a version number beside a genuine duration now silences it. Each
    of these was caveated `(개월, 주)` before the widening.

    `1.2.3 버전, 3주` still fires, because a one-digit first component is below
    the bound -- luck of where the bound fell, not a rule about version numbers,
    which is why it is asserted here rather than relied on.
    """
    from verinote.pipeline.query_intent import korean_measure_unit_mismatch

    question = "샘플사업의 기간은 몇 개월인가?"
    assert korean_measure_unit_mismatch(question, value) is None
    assert korean_measure_unit_mismatch(question, "1.2.3 버전, 3주") == ("개월", "주")


def test_달러_is_the_only_prefix_pair_that_crosses_canonical_units():
    """The premise the suppression ordering rests on, derived from the table.

    `_VALUE_MEASUREMENT_RELAXED` argues that dropping the lookahead is safe
    because sorting settles the one prefix pair whose order can change an
    answer. That argument is only as good as the premise, and nothing checked
    it: the ordering test's probes are chosen values, so a row introducing a
    second cross-unit prefix pair would falsify the paragraph and leave the
    suite green.

    Both counts are computed here rather than quoted, for the same reason the
    digit-prefix figure is.
    """
    from verinote.pipeline.query_intent import _MEASUREMENT_UNIT_SPELLINGS as spellings

    prefix_pairs = [
        (short, long)
        for short in spellings
        for long in spellings
        if short != long and long.startswith(short)
    ]
    crossing = [
        (short, long)
        for short, long in prefix_pairs
        if spellings[short] != spellings[long]
    ]
    assert len(prefix_pairs) == 10
    assert crossing == [("달", "달러")]


def test_a_unit_suffix_would_be_inert_here():
    """Why `_UNIT_SUFFIX` is absent from the relaxed pattern, re-derived.

    Not the general claim that a match's end is unread -- it is read, `finditer`
    resumes from it, and the two-line counterexample below shows an optional
    trailing group changing what a scan finds. The reason is specific to these
    character sets: every suffix member is Hangul and every match starts at a
    digit, so a moved end can never swallow a later match's start.

    Both halves are asserted, because only the pair is the argument: ends really
    do move, and readings really do not.
    """
    import re

    from verinote.pipeline.query_intent import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _UNIT_SUFFIX,
        _UNIT_SUFFIX_MEMBERS,
        _VALUE_MEASUREMENT_RELAXED,
    )

    # The general rule this reason is NOT: an optional trailing group does
    # change later matches when its characters can begin one.
    assert [m.group("u") for m in re.finditer(r"(?P<u>[ab])", "ab")] == ["a", "b"]
    assert [m.group("u") for m in re.finditer(r"(?P<u>[ab])(?:b)?", "ab")] == ["a"]

    # The premise, over the LIVE members rather than a copy of them. A literal
    # here would keep passing if a non-Hangul member were added, which is the
    # one change that breaks the argument: a digit member makes `3년2주` read
    # `년` alone, and `몇 주인가?` against it answers "the verified value states
    # 주". The corpus below carries `<quantity><quantity>` shapes so that the
    # readings loop sees it too, not only this assertion.
    assert _UNIT_SUFFIX_MEMBERS, "an empty member set would make this vacuous"
    for member in _UNIT_SUFFIX_MEMBERS:
        assert re.fullmatch(r"[가-힣]+", member), member

    # Both patterns are built from parts rather than by appending to the shipped
    # one. Deriving `with_suffix` from `_VALUE_MEASUREMENT_RELAXED.pattern` made
    # this test fail if the suffix were ever put back -- it would then be
    # comparing one suffix against two, the ends would stop moving, and the
    # non-vacuity guard below would fire on a mutation that breaks nothing. The
    # claim is about these character sets, not about today's pattern text.
    quantity = (
        r"[0-9][0-9,.]*\s*[만억천조]?\s*(?P<unit>"
        + "|".join(
            re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
        )
        + r")"
    )
    without_suffix = re.compile(quantity)
    with_suffix = re.compile(quantity + _UNIT_SUFFIX)
    corpus = [
        f"{number}{magnitude}{spelling}{tail}"
        for number in ("3", "1000")
        for magnitude in ("", "만")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        # The last four are the distinguishing shape: something follows the
        # suffix position, so a member that could begin a match would swallow
        # it. Without them the loop below cannot see a bad member.
        for tail in (
            "", "간", "가량", "정도", "쯤", "짜리", "차", "의", " ", "5", "s", "러",
            "2주", "간5주", "가량10초", "5초",
        )
    ]
    moved = sum(
        1 for value in corpus
        if [m.end() for m in with_suffix.finditer(value)]
        != [m.end() for m in without_suffix.finditer(value)]
    )
    assert moved > 0, "the suffix must actually match somewhere, or this is vacuous"
    for value in corpus:
        assert [m.group("unit") for m in with_suffix.finditer(value)] == [
            m.group("unit") for m in without_suffix.finditer(value)
        ], value

    # And the shipped pattern is the suffix-free one, which is what the comment
    # beside it claims. Asserted on readings, not on pattern text, so restoring
    # the suffix -- inert, per the loop above -- would not fail this.
    for value in corpus:
        assert [m.group("unit") for m in _VALUE_MEASUREMENT_RELAXED.finditer(value)] == [
            m.group("unit") for m in without_suffix.finditer(value)
        ], value
