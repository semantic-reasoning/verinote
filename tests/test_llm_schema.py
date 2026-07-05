# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm import LLMError
from verinote.llm.schema import EXTRACTION_SYSTEM, FACT_OBJECT_SCHEMA, parse_facts


def test_fact_schema_requires_every_property_for_strict_outputs():
    assert set(FACT_OBJECT_SCHEMA["required"]) == set(FACT_OBJECT_SCHEMA["properties"])


def test_extraction_prompt_extracts_verbatim_source_text():
    assert "verbatim from the source document" in EXTRACTION_SYSTEM
    assert "exact language, spelling, script, casing, and wording" in EXTRACTION_SYSTEM
    assert "do not translate" in EXTRACTION_SYSTEM
    assert "paraphrase" in EXTRACTION_SYSTEM


def test_extraction_prompt_biases_toward_explicit_fact_recall():
    assert "Extract all explicit factual triples" in EXTRACTION_SYSTEM
    assert "not only the most important ones" in EXTRACTION_SYSTEM
    assert "many small source-backed triples" in EXTRACTION_SYSTEM
    assert "For each sentence, table row, or bullet" in EXTRACTION_SYSTEM
    assert "Do not omit explicit facts" in EXTRACTION_SYSTEM


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
