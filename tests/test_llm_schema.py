# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm import LLMError
from verinote.llm.schema import FACT_OBJECT_SCHEMA, parse_facts


def test_fact_schema_requires_every_property_for_strict_outputs():
    assert set(FACT_OBJECT_SCHEMA["required"]) == set(FACT_OBJECT_SCHEMA["properties"])


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
