# SPDX-License-Identifier: MPL-2.0
"""Shared structured-output contract + parser used by every adapter.

Keeping the JSON schema and the parse-into-`ExtractedFact` logic in one place is
what makes the adapters thin: each provider only has to deliver text/JSON that
satisfies FACT_ARRAY_SCHEMA; `parse_facts` does the normalisation once.
"""

from __future__ import annotations

import json
from typing import Any

from verinote.llm.base import ExtractedFact, LLMError
from verinote.engine.terms import Compound, Term, TermParseError, Var, parse_term

# JSON Schema for a batch of extracted facts. Adapters pass this to whatever
# structured-output mechanism the provider offers (tool use / response_format /
# json mode) so the model is constrained to emit exactly this shape.
FACT_SLOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind", "value"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["string", "term"]},
        "value": {"type": "string", "minLength": 1},
    },
}

FACT_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["subject", "relation", "object", "confidence", "note"],
    "additionalProperties": False,
    "properties": {
        "subject": FACT_SLOT_SCHEMA,
        "relation": FACT_SLOT_SCHEMA,
        "object": FACT_SLOT_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "note": {"type": "string"},
    },
}

FACT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["facts"],
    "additionalProperties": False,
    "properties": {"facts": {"type": "array", "items": FACT_OBJECT_SCHEMA}},
}

EXTRACTION_SYSTEM = (
    "You extract source-backed factual triples from a document. Return ONLY facts "
    "stated or directly entailed by the text. Each fact is a (subject, relation, "
    "object) triple with a confidence in [0,1]. The subject, relation, and object "
    "must each be an object {\"kind\":\"string|term\", \"value\":\"...\"}. Use "
    "kind=\"term\" only for explicit, fully ground Datalog terms such as "
    "person(\"Ada\") or role(person(\"Ada\"), \"PI\"); otherwise use kind=\"string\". "
    "Use note=\"\" when there is no extra source note. Do not invent facts. Emit JSON "
    "matching the provided schema."
)


# --- query translation (#3) ------------------------------------------------
# A single Datalog query line: a rule deriving the question's answer relation,
# or a review_required(...) fallback when the question can't be expressed.
QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["datalog"],
    "additionalProperties": False,
    "properties": {"datalog": {"type": "string", "minLength": 1}},
}


def query_system(qid: int) -> str:
    """System prompt for translating one question to a Datalog query line."""
    return (
        "You translate a natural-language question into ONE line of DuckDB-supported Datalog "
        "over the base relation relation(subject, rel, object), which holds the "
        "knowledge base's confirmed facts. Produce a rule whose head is exactly "
        f"answer_q{qid}(V) binding a single answer variable V, for example:\n"
        f'  answer_q{qid}(O) :- relation("Ada Lovelace", "born_in", O).\n'
        "Use only the relation/3 predicate in rule bodies. Terms may be variables, "
        "string literals, integer literals, atoms, or fully ground compound terms; "
        "do not construct compound terms from variables in the answer head or body. "
        "If the question cannot be expressed this way, return exactly "
        'review_required("<the original question>"). Emit JSON matching the schema.'
    )


def parse_query(raw: str | dict[str, Any]) -> str:
    """Parse provider output into a single trimmed Datalog query line."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        line = str(data["datalog"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LLMError(f"query translation did not match schema: {exc}") from exc
    if not line:
        raise LLMError("query translation was empty")
    return line


def parse_facts(raw: str | dict[str, Any]) -> list[ExtractedFact]:
    """Parse provider output (a JSON string or already-decoded dict) into facts."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        items = data["facts"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LLMError(f"extractor output did not match schema: {exc}") from exc

    facts: list[ExtractedFact] = []
    for item in items:
        try:
            subject, subject_kind = _parse_fact_slot(item, "subject")
            relation, relation_kind = _parse_fact_slot(item, "relation")
            obj, object_kind = _parse_fact_slot(item, "object")
            facts.append(
                ExtractedFact(
                    subject=subject,
                    relation=relation,
                    object=obj,
                    confidence=float(item["confidence"]),
                    note=str(item.get("note", "")).strip(),
                    subject_kind=subject_kind,
                    relation_kind=relation_kind,
                    object_kind=object_kind,
                )
            )
        except (KeyError, TypeError, ValueError, TermParseError) as exc:
            raise LLMError(f"malformed fact object {item!r}: {exc}") from exc
    return facts


def _parse_fact_slot(item: dict[str, Any], field: str) -> tuple[str, str]:
    raw = item[field]
    if isinstance(raw, str):
        value = raw.strip()
        kind = "string"
    elif isinstance(raw, dict):
        kind = str(raw["kind"]).strip()
        value = str(raw["value"]).strip()
    else:
        raise TypeError(f"{field} must be a string or {{kind,value}} object")
    if kind not in {"string", "term"}:
        raise ValueError(f"{field}.kind must be 'string' or 'term'")
    if not value:
        raise ValueError(f"{field} was empty")
    if kind == "term":
        term = parse_term(value)
        if not _is_ground_term(term):
            raise TermParseError(f"{field} structural term must be ground")
    return value, kind


def _is_ground_term(term: Term) -> bool:
    if isinstance(term, Var):
        return False
    if isinstance(term, Compound):
        return all(_is_ground_term(arg) for arg in term.args)
    return True
