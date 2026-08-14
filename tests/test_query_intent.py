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


_ENGLISH_TAIL_PREDICATE_FIXTURES = {
    "called": "What is Sample Project's owner called?",
    "named": "What is Sample Project's owner named?",
    "titled": "What is Sample Project's owner titled?",
    "labelled": "What is Sample Project's owner labelled?",
    "labeled": "What is Sample Project's owner labeled?",
    "spelled": "What is Sample Project's owner spelled?",
    "spelt": "What is Sample Project's owner spelt?",
    "known as": "What is Sample Project's owner known as?",
    "listed as": "What is Sample Project's owner listed as?",
    "set to": "What is Sample Project's owner set to?",
}
"""One question per member of `_ENGLISH_ATTRIBUTE_TAIL_PREDICATE_MEMBERS`.

A literal dict plus a set-equality assertion, not a parametrize over the live
tuple: parametrizing would delete the case along with the member and the test
would pass vacuously. Reconstructing the questions from the tuple would be the
same hole one step further away -- an unchecked re-implementation of the rule.
Same shape as `_MONTH_WORD_FIXTURES` in tests/test_query_measure_unit.py, for
the same reason.

The possessive spelling, which is the one this rule reaches. The `of` spelling
puts the same tail on the entity instead of on the label, where the label
cleaner cannot see it, and this rule deliberately leaves it there. Refs #515,
and `tests/test_ask.py::test_the_of_shape_entity_keeps_its_trailing_predicate_at_the_answer`
pins that at the answer.
"""

_ENGLISH_TAIL_ADVERB_FIXTURES = {
    "also": "What is Sample Project's owner also known as?",
    "otherwise": "What is Sample Project's owner otherwise called?",
    "formerly": "What is Sample Project's owner formerly named?",
    "previously": "What is Sample Project's owner previously titled?",
    "originally": "What is Sample Project's owner originally labelled?",
    "currently": "What is Sample Project's owner currently listed as?",
    "commonly": "What is Sample Project's owner commonly spelled?",
    "officially": "What is Sample Project's owner officially set to?",
}
"""One question per member of `_ENGLISH_ATTRIBUTE_TAIL_ADVERB_MEMBERS`.

Literal for the reason above. Each adverb sits in front of a different
predicate so that a member surviving only in front of one predicate cannot hide
behind a uniform pairing.
"""


def test_every_trailing_naming_predicate_is_read_as_a_predicate():
    """A tail that says how the asking is phrased is not part of what is asked for.

    Before this rule the parser stripped none of these -- #511 reads the tree as
    already handling `called` and `named`, and it does not:
    `_clean_english_attribute_label` normalised whitespace and dropped a leading
    `the ` and nothing else. So `What is Sample Project's owner called?` asked
    for a relation literally named `owner called`, which no schema is expected
    to hold, and the question was planned empty.

    The set-equality assertion is what stops a member being added to the tuple
    without a question exercising it.
    """
    from verinote.pipeline.query_intent import (
        _ENGLISH_ATTRIBUTE_TAIL_PREDICATE_MEMBERS,
    )

    assert set(_ENGLISH_TAIL_PREDICATE_FIXTURES) == set(
        _ENGLISH_ATTRIBUTE_TAIL_PREDICATE_MEMBERS
    )
    for member, question in _ENGLISH_TAIL_PREDICATE_FIXTURES.items():
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, member
        assert intent.subject == IntentTarget("entity", "Sample Project"), member
        assert intent.relation_candidates == ("owner",), member


def test_every_tail_adverb_is_read_with_the_predicate():
    """`owner also known as` asks for `owner`, not for `owner also`.

    The adverb slot only ever extends a cut the predicate alternation already
    makes, so dropping a member here leaves its question stopping one word
    short: `('owner also',)` instead of `('owner',)`. That is still a relation
    no schema is expected to hold, so the failure is silent without this test.
    """
    from verinote.pipeline.query_intent import _ENGLISH_ATTRIBUTE_TAIL_ADVERB_MEMBERS

    assert set(_ENGLISH_TAIL_ADVERB_FIXTURES) == set(
        _ENGLISH_ATTRIBUTE_TAIL_ADVERB_MEMBERS
    )
    for adverb, question in _ENGLISH_TAIL_ADVERB_FIXTURES.items():
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, adverb
        assert intent.subject == IntentTarget("entity", "Sample Project"), adverb
        assert intent.relation_candidates == ("owner",), adverb


def test_a_word_a_predicate_only_ends_is_left_alone():
    """The tail binds with `\\s+`, so a member inside a longer word is not a tail.

    These ten are chosen against the two known relaxations of that binding,
    which are not the same one and are not caught by the same witness:

    - `\\s*` has no boundary requirement at all and cuts all ten below --
      `recalled` to `re`, `nicknamed` to `nick`, `untitled` to `un`. A corpus of
      `dataset`/`offset`/`blacklisted` would not notice: those end in `set` and
      `listed`, which are the *first* words of `set to` and `listed as` and are
      not members on their own, so the `\\s*` mutant leaves them whole and the
      test passes vacuously against it.
    - `\\s*\\b` spares nine of them, because `_` and every letter are word
      characters -- `so_called` is the row that needs the `_` -- and still cuts
      `re-called` to `re-`, because `-` is not.

    So `re-called` is the witness that tells the second relaxation from the
    first, and the other nine are what make the first one red at all.
    """
    for label in (
        "recalled",
        "unnamed",
        "renamed",
        "entitled",
        "misspelled",
        "nicknamed",
        "subtitled",
        "untitled",
        "so_called",
        "re-called",
    ):
        intent = deterministic_query_intent(f"What is Sample Project's {label}?")

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, label
        assert intent.relation_candidates == (label,), label


def test_a_past_participle_relation_name_survives():
    """A relation name whose last word is a past participle is not a tail.

    Column headers are full of them, and none is a member. This is the test that
    refuses the tempting generalisation -- `\\s+[A-Za-z]+(?:ed|en)(?:\\s+(?:as|to))?$`
    instead of an enumerated list -- which cuts `date created` to `date`,
    `last modified` to `last` and `amount owed` to `amount`. That mutant fails
    in the other direction too: it misses `known as`, `set to` and `spelt`,
    none of which is a participle ending in `ed`/`en` followed by a particle.
    """
    for label in (
        "date created",
        "last modified",
        "last updated",
        "tasks completed",
        "issues closed",
        "amount owed",
        "budget approved",
        "seats reserved",
        "hours logged",
        "items shipped",
        "invoices paid",
        "features shipped",
        "date resolved",
        "units sold",
        "records seen",
    ):
        intent = deterministic_query_intent(f"What is Sample Project's {label}?")

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, label
        assert intent.relation_candidates == (label,), label


def test_a_multiword_predicate_is_not_cut_to_its_last_word():
    """`known as`, `listed as` and `set to` are members only with their particle.

    Matching the participle alone would be the cheaper pattern and it takes
    ordinary relation names apart: `data set` becomes `data`, `well known`
    becomes `well`, `publicly listed` becomes `publicly`. The same holds for
    splitting the strip into two `sub` calls -- one for the particle, one for
    the participle -- which is why the rule is a single alternation against a
    single `$`.

    The last row is the positive that stops this test from being satisfiable by
    simply dropping the three multi-word members.
    """
    for label in (
        "data set",
        "target set",
        "character set",
        "test set",
        "result set",
        "publicly listed",
        "well known",
        "widely known",
    ):
        intent = deterministic_query_intent(f"What is Sample Project's {label}?")

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, label
        assert intent.relation_candidates == (label,), label

    cut = deterministic_query_intent("What is Sample Project's status set to?")
    assert cut.relation_candidates == ("status",)


def test_worth_and_like_stay_in_the_label():
    """#511's two deliberate exclusions, pinned as exclusions.

    `stock worth` is a plausible relation name whose last word belongs to the
    measure, and `owner like` asks for a description rather than for the object
    of `owner`, so stripping either would answer a different question.

    Spelled as `What is ...`, not as the issue's `How much is Sample Product's
    stock worth?`: that spelling is not one the possessive pattern claims at
    all, so a test written on it would report `unknown_or_unsupported` whatever
    the tuple holds, and would stay green with `worth` added as a member.
    """
    worth = deterministic_query_intent("What is Sample Product's stock worth?")
    assert worth.kind == QueryIntentKind.LOOKUP_OBJECT
    assert worth.relation_candidates == ("stock worth",)

    like = deterministic_query_intent("What is Sample Project's owner like?")
    assert like.kind == QueryIntentKind.LOOKUP_OBJECT
    assert like.relation_candidates == ("owner like",)


def test_the_strip_cannot_leave_the_field_empty():
    """A question whose whole label is a predicate keeps a label.

    The invariant is a property of the pattern, not of the tuple: both fields
    are `.strip()`ed before the pattern is applied, so position 0 is never
    whitespace, and the pattern must consume at least one leading `\\s`, so at
    least one character always survives. "No member empties itself" would be the
    same claim resting on today's list, and it would stop holding the day a
    member is added that a caller can hand over whole.

    Not empty is not the same as not rubbish, and the rows below are the
    difference: `the the called` comes back as `the`, a relation name no schema
    is expected to hold. The accepted-cost block at the end carries the rest of
    that population, including the rows where the residue is *not* harmless.
    """
    for question, expected in (
        ("What is Sample Project's called?", "called"),
        ("What is Sample Project's known as?", "known as"),
        ("What is Sample Project's set to?", "set to"),
        ("What is Sample Project's the called?", "called"),
        ("What is Sample Project's the listed as?", "listed as"),
        ("What is Sample Project's the the called?", "the"),
    ):
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, question
        assert intent.subject == IntentTarget("entity", "Sample Project"), question
        assert intent.relation_candidates == (expected,), question

    # The rule's accepted cost, pinned so that it reads as measured rather than
    # as overlooked. `also known as` is a real attribute name and the strip
    # takes it to `also`; `so called` is the same shape. These are read *worse*
    # than before the rule.
    #
    # The last three rows are the sharper half of that cost and they are not
    # interchangeable with the first two. `also` and `so` are relation names no
    # schema is expected to hold, so those questions still decline, just one
    # word further along -- unless the schema holds the name the question
    # actually spells, `also known as`, in which case the answer that used to
    # be free is given up instead. `date`, `user` and `hand` are relation names
    # a schema may really hold, and where it does the question stops declining:
    # measured against a KB carrying a `date` relation, `date labeled` moves
    # from `review_required` plus one provider call to `translated` with none,
    # answering what the date is for a question asking what it is labeled.
    # `date labeled` differs by one word from the `date created` that
    # `test_a_past_participle_relation_name_survives` protects.
    #
    # So this block, not just the possessive-entity test, is where the change
    # admits it can promote a question rather than only shorten a label.
    # Anyone narrowing the rule to recover these should expect this red.
    #
    # The six rows are witnesses, not the population. `self titled` -> `self`
    # and `correctly spelled` -> `correctly` are the same class as
    # `also known as` -> `also` and `hand labeled` -> `hand`, and neither is
    # listed here. The population is every label whose last word or two is a
    # member, which is open by construction because the tuple is.
    for question, expected in (
        ("What is Sample Person's also known as?", "also"),
        ("What is the also known as of Sample Person?", "also"),
        ("What is Sample Project's so called?", "so"),
        ("What is Sample Dataset's date labeled?", "date"),
        ("What is Sample Record's user named?", "user"),
        ("What is Sample Dataset's hand labeled?", "hand"),
    ):
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, question
        assert intent.relation_candidates == (expected,), question


def test_a_shortened_label_re_enters_the_purpose_synonyms():
    """The strip changes which questions reach the synonym table, not only labels.

    `purpose titled` is not a key in `_attribute_relation_candidates`, so before
    this rule the question asked for exactly one relation candidate. Cleaned to
    `purpose`, it is a key, and the same question now asks for all five. So
    "the rule only shortens a label, the result set is unchanged" is false, and
    this is the measurement that says so.
    """
    for question in (
        "What is Sample Project's purpose titled?",
        "What is Sample Project's goal known as?",
        "What is Sample Project's objective called?",
    ):
        intent = deterministic_query_intent(question)

        assert intent.kind == QueryIntentKind.LOOKUP_OBJECT, question
        assert intent.relation_candidates == PURPOSE_RELATION_CANDIDATES, question
