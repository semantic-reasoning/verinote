# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm import LLMError
from verinote.llm.schema import (
    EXTRACTION_SYSTEM,
    FACT_OBJECT_SCHEMA,
    QUERY_INTENT_SCHEMA,
    parse_facts,
)
from verinote.pipeline.query_intent import IntentTarget, QueryIntentKind, parse_query_intent


def _intent_payload(**overrides):
    payload = {
        "kind": "unknown_or_unsupported",
        "subject": None,
        "relation": None,
        "object": None,
        "relation_candidates": None,
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": "unsupported",
    }
    payload.update(overrides)
    return payload


def test_fact_schema_requires_every_property_for_strict_outputs():
    assert set(FACT_OBJECT_SCHEMA["required"]) == set(FACT_OBJECT_SCHEMA["properties"])


def test_query_intent_schema_is_separate_from_datalog_query_schema():
    # Every property stays in `required`, `reason` included. OpenAI structured
    # outputs run this schema with `strict: True` (openai_adapter.py), which
    # forbids both optional properties and per-kind conditional requirements.
    # Dropping `reason` from `required` to express "only unknown_or_unsupported
    # needs a reason" would silently break that adapter; the reason contract is
    # carried by the prompt and the intent validator instead (issue #237).
    assert QUERY_INTENT_SCHEMA["required"] == list(QUERY_INTENT_SCHEMA["properties"])
    assert "datalog" not in QUERY_INTENT_SCHEMA["properties"]
    assert QUERY_INTENT_SCHEMA["additionalProperties"] is False
    assert (
        "discover_entity_relations"
        in QUERY_INTENT_SCHEMA["properties"]["kind"]["enum"]
    )
    for field in ("subject", "relation", "object"):
        schema = QUERY_INTENT_SCHEMA["properties"][field]
        assert schema["type"] == ["object", "null"]
        assert schema["required"] == list(schema["properties"])
        assert schema["additionalProperties"] is False
    assert QUERY_INTENT_SCHEMA["properties"]["value_type"]["enum"] == [
        "date",
        "number",
        "amount",
        "ordinal",
        None,
    ]


def test_parse_query_intent_accepts_valid_lookup_object():
    intent = parse_query_intent(
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물"},
            relation_candidates=["역할", "직책", "직위"],
            reason=None,
        )
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.subject == IntentTarget("entity", "샘플인물")
    assert intent.relation_candidates == ("역할", "직책", "직위")


def test_parse_query_intent_accepts_valid_lookup_subject_lookup_relation_and_count():
    assert (
        parse_query_intent(
            _intent_payload(
                kind="lookup_subject",
                relation={"kind": "relation", "value": "역할"},
                object={"kind": "entity", "value": "검토자"},
                reason=None,
            )
        ).kind
        == QueryIntentKind.LOOKUP_SUBJECT
    )
    assert (
        parse_query_intent(
            _intent_payload(
                kind="lookup_relation",
                subject={"kind": "entity", "value": "샘플인물"},
                object={"kind": "entity", "value": "샘플문서"},
                reason=None,
            )
        ).kind
        == QueryIntentKind.LOOKUP_RELATION
    )
    assert (
        parse_query_intent(
            _intent_payload(
                kind="count",
                relation={"kind": "relation", "value": "참여"},
                reason=None,
            )
        ).kind
        == QueryIntentKind.COUNT
    )


def test_parse_query_intent_accepts_valid_compare_typed_value():
    intent = parse_query_intent(
        _intent_payload(
            kind="compare_typed_value",
            subject={"kind": "entity", "value": "샘플항목"},
            relation={"kind": "relation", "value": "수량"},
            operator=">",
            value_type="number",
            value="10",
            reason=None,
        )
    )

    assert intent.kind == QueryIntentKind.COMPARE_TYPED_VALUE
    assert intent.operator == ">"
    assert intent.value_type == "number"
    assert intent.value == "10"


def test_parse_query_intent_accepts_entity_relation_discovery():
    broad = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            reason=None,
        )
    )
    direct_first = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation={"kind": "relation", "value": "provides"},
            reason=None,
        )
    )
    candidate_first = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation_candidates=["provides", "connects"],
            reason=None,
        )
    )

    assert broad.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert broad.subject == IntentTarget("entity", "Sample Entity")
    assert broad.relation_candidates == ()
    assert direct_first.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert direct_first.relation == IntentTarget("relation", "provides")
    assert candidate_first.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert candidate_first.relation_candidates == ("provides", "connects")


def test_parse_query_intent_accepts_unsupported_with_reason():
    intent = parse_query_intent(
        _intent_payload(reason="requires planning")
    )

    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    assert intent.reason == "requires planning"


_REASON_TOLERANT_INTENTS = [
    (
        "lookup_object",
        {
            "subject": {"kind": "entity", "value": "샘플인물"},
            "relation": {"kind": "relation", "value": "역할"},
        },
    ),
    (
        "lookup_subject",
        {
            "relation": {"kind": "relation", "value": "역할"},
            "object": {"kind": "entity", "value": "샘플직책"},
        },
    ),
    (
        "lookup_relation",
        {"subject": {"kind": "entity", "value": "샘플인물"}},
    ),
    (
        "discover_entity_relations",
        {"subject": {"kind": "entity", "value": "Sample Entity"}},
    ),
    (
        "count",
        {"subject": {"kind": "entity", "value": "샘플인물"}},
    ),
    (
        "compare_typed_value",
        {
            "subject": {"kind": "entity", "value": "샘플항목"},
            "relation": {"kind": "relation", "value": "수량"},
            "operator": ">",
            "value_type": "number",
            "value": "10",
        },
    ),
]


@pytest.mark.parametrize(
    ("kind", "fields"),
    _REASON_TOLERANT_INTENTS,
    ids=[kind for kind, _ in _REASON_TOLERANT_INTENTS],
)
def test_parse_query_intent_accepts_advisory_reason_on_every_kind(kind, fields):
    """A reason on a well-formed non-unknown intent is advisory, not a violation.

    The schema requires every property, so a provider that fills `reason` while
    classifying correctly used to have its whole answer rejected as off-schema
    (issue #237). Only `unknown_or_unsupported` reads `reason`, so tolerating it
    elsewhere cannot change the resulting query.
    """
    intent = parse_query_intent(
        _intent_payload(kind=kind, reason="classified from the question wording", **fields)
    )

    assert intent.kind == QueryIntentKind(kind)
    assert intent.reason == "classified from the question wording"


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_query_intent_treats_a_blank_nullable_string_as_null(blank):
    """A blank `reason` means "no reason", exactly like an omitted key.

    `claude_cli_adapter` renders the schema into the prompt rather than
    constraining decoding, so `minLength: 1` is never enforced -- and the schema
    demands the `reason` key while the prompt says to leave it null. A model
    splitting that difference with `""` is routine, and rejecting it would kill a
    correctly classified question for the very reason issue #237 did.
    """
    intent = parse_query_intent(
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물"},
            relation={"kind": "relation", "value": "역할"},
            reason=blank,
        )
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.reason is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_query_intent_still_rejects_unknown_with_a_blank_reason(blank):
    """Normalizing blank to null must not let a reasonless unknown through."""
    with pytest.raises(LLMError):
        parse_query_intent(_intent_payload(kind="unknown_or_unsupported", reason=blank))


def test_parse_query_intent_still_rejects_compare_with_a_blanked_out_field():
    """A blank comparison field is an absent one, and compare_typed_value needs it."""
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="compare_typed_value",
                subject={"kind": "entity", "value": "샘플항목"},
                relation={"kind": "relation", "value": "수량"},
                operator="",
                value_type="number",
                value="10",
            )
        )


def test_parse_query_intent_treats_a_missing_nullable_key_as_null():
    """Prompt-only providers render the schema as text and drop null keys.

    `claude_cli` only renders the schema into the prompt, so an omitted key is
    routine; the schema already declares null legal for every nullable field, so
    "absent" and "null" have to mean the same thing. `kind` stays required.
    """
    intent = parse_query_intent(
        {
            "kind": "lookup_object",
            "subject": {"kind": "entity", "value": "샘플인물"},
            "relation": {"kind": "relation", "value": "역할"},
        }
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.subject == IntentTarget("entity", "샘플인물")
    assert intent.reason is None


def test_parse_query_intent_still_requires_a_reason_for_unknown():
    with pytest.raises(LLMError):
        parse_query_intent(_intent_payload(kind="unknown_or_unsupported", reason=None))


def test_parse_query_intent_still_rejects_unknown_carrying_query_fields():
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="unknown_or_unsupported",
                subject={"kind": "entity", "value": "샘플인물"},
                reason="requires planning",
            )
        )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        {"subject": {"kind": "entity", "value": "샘플인물"}},
        _intent_payload(kind="not_a_kind"),
        _intent_payload(kind="lookup_object", unexpected="field"),
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물", "extra": "x"},
            relation={"kind": "relation", "value": "역할"},
            reason=None,
        ),
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": ""},
            relation={"kind": "relation", "value": "역할"},
            reason=None,
        ),
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물"},
            relation_candidates=["역할", 3],
            reason=None,
        ),
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물"},
            reason=None,
        ),
        _intent_payload(reason=""),
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "relation", "value": "역할"},
            relation={"kind": "entity", "value": "샘플인물"},
            reason=None,
        ),
        _intent_payload(
            kind="lookup_subject",
            relation={"kind": "entity", "value": "샘플인물"},
            object={"kind": "relation", "value": "역할"},
            reason=None,
        ),
        _intent_payload(
            kind="lookup_relation",
            relation={"kind": "relation", "value": "역할"},
            subject={"kind": "entity", "value": "샘플인물"},
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject=None,
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "relation", "value": "provides"},
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            object={"kind": "entity", "value": "Sample Object"},
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            operator=">",
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation={"kind": "relation", "value": "provides"},
            relation_candidates=["offers"],
            reason=None,
        ),
        _intent_payload(
            kind="compare_typed_value",
            subject={"kind": "entity", "value": "샘플항목"},
            relation={"kind": "relation", "value": "수량"},
            operator=">",
            value_type="duration",
            value="10",
            reason=None,
        ),
    ],
)
def test_parse_query_intent_rejects_malformed_or_invalid_output(raw):
    with pytest.raises(LLMError):
        parse_query_intent(raw)


def test_extraction_prompt_prioritizes_semantic_spo_facts():
    assert "semantic subject-predicate-object statement" in EXTRACTION_SYSTEM
    assert "Subject is the entity, row key, or record owner" in EXTRACTION_SYSTEM
    assert "Relation is a concise predicate" in EXTRACTION_SYSTEM
    assert "Object is the related entity or value" in EXTRACTION_SYSTEM
    assert "instead of copying whole source phrases" in EXTRACTION_SYSTEM
    assert "named-entity spelling for subjects and objects" in EXTRACTION_SYSTEM
    assert "prefer concise English canonical predicates" in EXTRACTION_SYSTEM
    assert "such as `role`, `affiliation`, `provides`, or `value`" in EXTRACTION_SYSTEM
    assert "exact original supporting phrase in note" in EXTRACTION_SYSTEM
    assert "merely because two entities appear in the same chunk" in EXTRACTION_SYSTEM
    assert "numeric, percentage, count, date, money" in EXTRACTION_SYSTEM
    assert "same local evidence record" in EXTRACTION_SYSTEM
    assert "key-value or label-value text" in EXTRACTION_SYSTEM
    assert "use relation `value`" in EXTRACTION_SYSTEM
    assert "Do not use `is_a` unless" in EXTRACTION_SYSTEM
    assert "sentence endings such as `입니다`" in EXTRACTION_SYSTEM
    assert "predicates ending in `여부`" in EXTRACTION_SYSTEM


def test_extraction_prompt_biases_toward_explicit_fact_recall():
    assert "Extraction is not a summary" in EXTRACTION_SYSTEM
    assert "Traverse every visible section, table, list" in EXTRACTION_SYSTEM
    assert "Do not sample representative items" in EXTRACTION_SYSTEM
    assert "many small source-backed triples" in EXTRACTION_SYSTEM
    assert "For each sentence, table row, bullet, list item, or layout record" in EXTRACTION_SYSTEM
    assert "Self-check before returning" in EXTRACTION_SYSTEM


def test_extraction_prompt_uses_factlog_table_mapping_rules():
    assert "Extract tables and structured records row by row" in EXTRACTION_SYSTEM
    assert "Use the row-identifying key" in EXTRACTION_SYSTEM
    assert "as subject" in EXTRACTION_SYSTEM
    assert "Use the column header or item label as relation" in EXTRACTION_SYSTEM
    assert "Use the cell value as object" in EXTRACTION_SYSTEM
    assert "emit separate facts" in EXTRACTION_SYSTEM
    assert "Do not include CSV headers" in EXTRACTION_SYSTEM


def test_extraction_prompt_prefers_typed_literal_terms():
    assert "`date(YYYY)`" in EXTRACTION_SYSTEM
    assert "`ordinal(N)`" in EXTRACTION_SYSTEM
    assert "`amount(N,\"unit\")`" in EXTRACTION_SYSTEM
    assert "`number(N)`" in EXTRACTION_SYSTEM
    assert "Typed literals are object values, never subjects or relations" in EXTRACTION_SYSTEM
    assert "relation `number(8)` and object `명`" in EXTRACTION_SYSTEM
    assert "Entity objects" in EXTRACTION_SYSTEM
    assert "must remain plain strings" in EXTRACTION_SYSTEM


def test_parse_facts_accepts_legacy_string_slots():
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": 'person("Ada")',
                    "relation": "has_role",
                    "object": 'role(person("Ada"), "PI")',
                    "confidence": 0.9,
                }
            ]
        }
    )

    fact = facts[0]
    assert fact.subject == 'person("Ada")'
    assert fact.relation == "has_role"
    assert fact.object == 'role(person("Ada"), "PI")'
    assert (fact.subject_kind, fact.relation_kind, fact.object_kind) == (
        "string",
        "string",
        "string",
    )


def test_parse_facts_accepts_top_level_array_from_local_models():
    facts = parse_facts(
        [
            {
                "subject": "Ada",
                "relation": "is_a",
                "object": "mathematician",
                "confidence": 0.9,
                "note": "",
            }
        ]
    )

    assert facts[0].subject == "Ada"
    assert facts[0].relation == "is_a"
    assert facts[0].object == "mathematician"


def test_parse_facts_accepts_common_array_wrappers_from_local_models():
    for key in ("facts", "items", "data", "results"):
        facts = parse_facts(
            {
                key: [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    }
                ]
            }
        )

        assert [(fact.subject, fact.relation, fact.object) for fact in facts] == [
            ("Ada", "is_a", "mathematician")
        ]


def test_parse_facts_uses_first_json_value_from_noisy_local_output():
    facts = parse_facts(
        '```json\n'
        '[{"subject":"Ada","relation":"is_a","object":"mathematician",'
        '"confidence":0.9,"note":""}]\n'
        '```\n'
        '[{"subject":"ignored","relation":"ignored","object":"ignored",'
        '"confidence":0.1,"note":""}]'
    )

    assert [(fact.subject, fact.relation, fact.object) for fact in facts] == [
        ("Ada", "is_a", "mathematician")
    ]


def test_parse_facts_accepts_explicit_term_slots():
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": {"kind": "term", "value": 'person("Ada")'},
                    "relation": {"kind": "term", "value": "has_role"},
                    "object": {"kind": "term", "value": 'role(person("Ada"), "PI")'},
                    "confidence": 0.9,
                    "note": "source-backed",
                }
            ]
        }
    )

    fact = facts[0]
    assert fact.subject == 'person("Ada")'
    assert fact.relation == "has_role"
    assert fact.object == 'role(person("Ada"), "PI")'
    assert (fact.subject_kind, fact.relation_kind, fact.object_kind) == (
        "term",
        "term",
        "term",
    )
    assert fact.note == "source-backed"


def test_parse_facts_downgrades_invalid_term_slots_to_strings():
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": {"kind": "term", "value": "Example Corp"},
                    "relation": {"kind": "string", "value": "legal_representative"},
                    "object": {"kind": "term", "value": 'person("Ada")'},
                    "confidence": 1,
                    "note": "",
                }
            ]
        }
    )

    fact = facts[0]
    assert fact.subject == "Example Corp"
    assert fact.object == 'person("Ada")'
    assert (fact.subject_kind, fact.relation_kind, fact.object_kind) == (
        "string",
        "string",
        "term",
    )
    assert "subject marked term but stored as string" in fact.note


def test_parse_facts_downgrades_variable_bearing_term_slots_to_strings():
    fact = parse_facts(
        {
            "facts": [
                {
                    "subject": {"kind": "term", "value": "person(Name)"},
                    "relation": {"kind": "string", "value": "has_role"},
                    "object": {"kind": "string", "value": "PI"},
                    "confidence": 0.9,
                    "note": "",
                }
            ]
        }
    )[0]

    assert fact.subject_kind == "string"
    assert "structural term must be ground" in fact.note


@pytest.mark.parametrize(
    "slot",
    [
        {"kind": "bogus", "value": "Ada"},
        {"kind": "string", "value": ""},
        {"kind": "term"},
    ],
)
def test_parse_facts_rejects_malformed_explicit_slots(slot):
    with pytest.raises(LLMError):
        parse_facts(
            {
                "facts": [
                    {
                        "subject": slot,
                        "relation": "has_role",
                        "object": "PI",
                        "confidence": 0.9,
                    }
                ]
            }
        )


def test_parse_facts_accepts_batch_of_only_valid_facts():
    """A batch with no schema violations is parsed in full -- nothing raises."""
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": "Ada",
                    "relation": "wrote",
                    "object": "notes",
                    "confidence": 0.9,
                    "note": "",
                },
                {
                    "subject": "Ada",
                    "relation": "is_a",
                    "object": "mathematician",
                    "confidence": 0.9,
                    "note": "",
                },
            ]
        }
    )

    assert [(fact.subject, fact.relation, fact.object) for fact in facts] == [
        ("Ada", "wrote", "notes"),
        ("Ada", "is_a", "mathematician"),
    ]


def test_parse_facts_raises_when_valid_and_malformed_are_mixed():
    """A violation next to valid facts must fail the whole batch, not drop.

    Dropping the off-schema item while returning the valid one would report the
    chunk as a success with the violating fact silently gone. `base.py` promises
    `LLMError` on any schema violation so the caller can retry deterministically
    (issue #168), so the mixed batch raises rather than smuggling a partial
    result past the retry path.
    """
    with pytest.raises(LLMError):
        parse_facts(
            {
                "facts": [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": "Ada",
                        "relation": "",  # empty slot is a schema violation
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            }
        )


def test_parse_facts_raises_when_every_item_is_malformed():
    """A batch of only violations also fails loudly."""
    with pytest.raises(LLMError):
        parse_facts(
            {
                "facts": [
                    {
                        "subject": "",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": "Ada",
                        "relation": "",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            }
        )


def _widget_fact(**overrides):
    fact = {
        "subject": "Widget",
        "relation": "made_by",
        "object": "Gadget",
        "confidence": 0.9,
        "note": "",
    }
    fact.update(overrides)
    return fact


@pytest.mark.parametrize(
    "overrides",
    [
        # The repros as filed: an off-schema JSON type in a bare slot.
        {"subject": ["Widget", "Alpha"]},
        {"object": {"city": "Gadgetville"}},
        {"subject": None},
        {"subject": "   "},
        # The same off-schema types smuggled through the {kind,value} branch,
        # which str() used to coerce into plausible-looking review-queue text.
        {"subject": {"kind": "string", "value": ["Widget", "Alpha"]}},
        {"subject": {"kind": "string", "value": None}},
        {"subject": {"kind": "string", "value": "   "}},
        {"object": {"kind": "string", "value": {"city": "Gadgetville"}}},
        {"object": {"kind": "term", "value": {"functor": "maker"}}},
        {"relation": {"kind": ["string"], "value": "made_by"}},
        # `note` is declared {"type": "string"}.
        {"note": ["off", "schema"]},
        # `confidence` is declared {"type": "number", "minimum": 0, "maximum": 1}.
        {"confidence": True},
        {"confidence": 90},
        {"confidence": -1},
    ],
)
def test_parse_facts_rejects_off_schema_slot_types_instead_of_coercing(overrides):
    """The declared schema types every slot as a string; str() coercion would let
    lists/objects/null reach the review queue as text like "['Widget', 'Alpha']"."""
    with pytest.raises(LLMError):
        parse_facts({"facts": [_widget_fact(**overrides)]})


def test_parse_facts_still_accepts_plain_string_slots():
    fact = parse_facts({"facts": [_widget_fact()]})[0]

    assert (fact.subject, fact.relation, fact.object) == ("Widget", "made_by", "Gadget")
    assert fact.subject_kind == fact.relation_kind == fact.object_kind == "string"
    assert fact.confidence == 0.9


def test_parse_facts_still_accepts_explicit_ground_term_slots():
    """Fact-storage boundary: a structural fact is only a term when the model says
    so with {"kind": "term"}. Enforcing slot types must not break that path."""
    fact = parse_facts(
        {"facts": [_widget_fact(object={"kind": "term", "value": "maker(gadget)"})]}
    )[0]

    assert fact.object == "maker(gadget)"
    assert fact.object_kind == "term"
    assert fact.note == ""


def test_parse_facts_keeps_plain_functor_text_as_a_string_literal():
    """A bare "maker(gadget)" string is a StringLit, never reinterpreted as a compound."""
    fact = parse_facts({"facts": [_widget_fact(object="maker(gadget)")]})[0]

    assert fact.object == "maker(gadget)"
    assert fact.object_kind == "string"


@pytest.mark.parametrize("note", [None, "seen in the spec"])
def test_parse_facts_accepts_missing_or_null_note(note):
    payload = _widget_fact()
    if note is None:
        payload.pop("note")
    else:
        payload["note"] = note

    assert parse_facts({"facts": [payload]})[0].note == (note or "")


def test_parse_facts_tolerates_numeric_string_confidence_from_prompt_only_providers():
    assert parse_facts({"facts": [_widget_fact(confidence="0.75")]})[0].confidence == 0.75
