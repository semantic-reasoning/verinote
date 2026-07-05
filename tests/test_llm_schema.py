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
    assert QUERY_INTENT_SCHEMA["required"] == list(QUERY_INTENT_SCHEMA["properties"])
    assert "datalog" not in QUERY_INTENT_SCHEMA["properties"]
    assert QUERY_INTENT_SCHEMA["additionalProperties"] is False
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


def test_parse_query_intent_accepts_unsupported_with_reason():
    intent = parse_query_intent(
        _intent_payload(reason="requires planning")
    )

    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    assert intent.reason == "requires planning"


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
    assert "subject is the entity or row key being described" in EXTRACTION_SYSTEM
    assert "relation is a concise predicate" in EXTRACTION_SYSTEM
    assert "object is the related entity or value" in EXTRACTION_SYSTEM
    assert "instead of copying whole source phrases" in EXTRACTION_SYSTEM
    assert "named-entity spelling" in EXTRACTION_SYSTEM
    assert "do not translate" in EXTRACTION_SYSTEM
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
    assert "extraction is not a summary" in EXTRACTION_SYSTEM
    assert "Traverse every section, table, and list" in EXTRACTION_SYSTEM
    assert "Do not sample representative items" in EXTRACTION_SYSTEM
    assert "many small source-backed triples" in EXTRACTION_SYSTEM
    assert "For each sentence, table row, bullet, or layout record" in EXTRACTION_SYSTEM
    assert "Self-check before finishing" in EXTRACTION_SYSTEM


def test_extraction_prompt_uses_factlog_table_mapping_rules():
    assert "extract tables and structured records row by row" in EXTRACTION_SYSTEM
    assert "Use the row-identifying key" in EXTRACTION_SYSTEM
    assert "as subject" in EXTRACTION_SYSTEM
    assert "Use the column header or item label as relation" in EXTRACTION_SYSTEM
    assert "Use the cell value as object" in EXTRACTION_SYSTEM
    assert "emit separate facts" in EXTRACTION_SYSTEM


def test_extraction_prompt_prefers_typed_literal_terms():
    assert "`date(YYYY)`" in EXTRACTION_SYSTEM
    assert "`ordinal(N)`" in EXTRACTION_SYSTEM
    assert "`amount(N,\"unit\")`" in EXTRACTION_SYSTEM
    assert "`number(N)`" in EXTRACTION_SYSTEM
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


def test_parse_facts_skips_malformed_items_when_valid_facts_remain():
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": "Ada",
                    "relation": "",
                    "object": "mathematician",
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
        ("Ada", "is_a", "mathematician")
    ]
