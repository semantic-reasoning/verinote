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
    "You are a fact extractor. Do not summarize the document; record source-backed "
    "candidate relationships directly verifiable from the text. Return ONLY facts "
    "explicitly stated by the text.\n\n"
    "Completeness principle: extraction is not a summary. Traverse every section, "
    "table, and list through the end of the input. Do not sample representative "
    "items. If a document lists N participants, M organizations, K schedule rows, "
    "or many career, education, patent, budget, finance, registration, or project "
    "items, extract all explicit rows/items that are not prohibited private data. "
    "Do not stop at prose paragraphs; tables are usually the densest source of "
    "facts.\n\n"
    "Structured-data mapping: extract tables and structured records row by row. "
    "Use the row-identifying key, such as a name, organization, year, item, or "
    "record label, as subject. Use the column header or item label as relation. "
    "Use the cell value as object. If one item has multiple values, years, or "
    "units, emit separate facts and put the year/unit/context in note when it is "
    "not already part of the relation or object. For each sentence, table row, "
    "bullet, or layout record, extract every distinct subject-predicate-object "
    "fact that is explicitly stated.\n\n"
    "Fact shape: each row must contain exactly one reusable relationship "
    "proposition. Write each fact as a semantic subject-predicate-object statement: "
    "subject is the entity or row key being described, relation is a concise "
    "predicate derived from the source label/header/verb, and object is the related "
    "entity or value. Prefer many small source-backed triples over one broad "
    "summary triple. Do not emit broad, low-utility relations such as `관련 있음`. "
    "Do not use sentence endings such as `입니다` as objects. Do not emit question "
    "or judgment predicates ending in `여부`. Do not use `is_a` unless the object "
    "is a class, category, or type of the subject.\n\n"
    "Local evidence rule: do not infer a relationship merely because two entities "
    "appear in the same chunk. The relation must be explicit in the same sentence, "
    "table row, bullet, or layout record. For numeric, percentage, count, date, "
    "money, registration, or key-value facts, the subject must appear in that same "
    "local evidence record; do not reuse a subject from a previous or overlapping "
    "chunk.\n\n"
    "Normalization: normalize subjects, relations, and objects to concise "
    "source-language fact terms instead of copying whole source phrases. Preserve "
    "the source document's language, script, and named-entity spelling; do not "
    "translate, romanize, summarize, or replace source terms with labels from "
    "another language. If a fact is normalized, compacted, corrected, or derived "
    "from layout, put the exact original supporting phrase in note.\n\n"
    "Typed literals: when the object is a typed literal such as a date, money "
    "amount, ordinal/rank, or general number, prefer compact compound terms: "
    "`date(YYYY)`, `date(YYYY,M)`, `date(YYYY,M,D)`, `ordinal(N)`, "
    "`amount(N,\"unit\")`, or `number(N)`. Entity objects such as organization "
    "names, person names, product names, or technology names must remain plain "
    "strings, not compound terms.\n\n"
    "Schema: each fact is a (subject, relation, object) triple with confidence in "
    "[0,1]. The subject, relation, and object must each be an object "
    "{\"kind\":\"string|term\", \"value\":\"...\"}. Use "
    "kind=\"term\" only for explicit, fully ground Datalog terms such as "
    "person(\"Ada\") or role(person(\"Ada\"), \"PI\"). Bare names, labels, Korean, "
    "Chinese, whitespace, or punctuation outside quoted string arguments are not "
    "valid terms; use kind=\"string\" for those values. "
    "For key-value or label-value text, do not emit generic predicates like `값`; "
    "use relation `value` only when no clearer owner/predicate is available.\n\n"
    "Prohibited: do not extract API keys, private tokens, passwords, session "
    "secrets, resident registration numbers, phone numbers, email addresses, "
    "private account names, internal URLs, or relationships not present in the "
    "document.\n\n"
    "Self-check before finishing: ask whether any section, table, or list in the "
    "input was not covered. If any remains, extract it before returning. Use "
    "note=\"\" when there is no extra source note. Do not invent facts. Emit JSON "
    "matching the provided schema."
)


# --- query translation (#3) ------------------------------------------------
# A single Datalog query line: either a rule deriving the question's answer
# relation, or a durable non-executable outcome with a concise reason.
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
        "If no executable rule is appropriate, return a durable status line "
        "with a concise reason instead: review_required(\"reason\") when a human "
        "must clarify or model the query, no_answer(\"reason\") when confirmed "
        "facts cannot answer it, or ambiguous(\"reason\") when multiple meanings "
        "or entities fit. Emit JSON matching the schema."
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


# --- query intent extraction (#113) ---------------------------------------

QUERY_INTENT_TARGET_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "required": ["kind", "value"],
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["entity", "relation", "value", "typed_value"]},
        "value": {"type": "string", "minLength": 1},
    },
}

QUERY_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "lookup_object",
                "lookup_subject",
                "lookup_relation",
                "discover_entity_relations",
                "count",
                "compare_typed_value",
                "unknown_or_unsupported",
            ],
        },
        "subject": QUERY_INTENT_TARGET_SCHEMA,
        "relation": QUERY_INTENT_TARGET_SCHEMA,
        "object": QUERY_INTENT_TARGET_SCHEMA,
        "relation_candidates": {
            "type": ["array", "null"],
            "items": {"type": "string", "minLength": 1},
        },
        "operator": {"type": ["string", "null"], "enum": ["=", "!=", "<", "<=", ">", ">=", None]},
        "value_type": {
            "type": ["string", "null"],
            "enum": ["date", "number", "amount", "ordinal", None],
        },
        "value": {"type": ["string", "null"], "minLength": 1},
        "reason": {"type": ["string", "null"], "minLength": 1},
    },
}
QUERY_INTENT_SCHEMA["required"] = list(QUERY_INTENT_SCHEMA["properties"])


QUERY_INTENT_SYSTEM = (
    "Classify one natural-language question into a constrained query intent. "
    "Return only JSON matching the schema. Use null for fields that do not apply. "
    "Use discover_entity_relations for broad entity-centric relationship questions "
    "where the entity is known but the useful KB relation may need schema-backed "
    "discovery; include an optional relation or relation_candidates only when the "
    "question also gives a direct relation hint to try first. "
    "Do not emit Datalog, relation/3 rules, answer_q predicates, or execution plans."
)


def parse_facts(raw: str | list[Any] | dict[str, Any]) -> list[ExtractedFact]:
    """Parse provider output (a JSON string or already-decoded dict) into facts."""
    try:
        data = _decode_first_json(raw) if isinstance(raw, str) else raw
        items = _fact_items(data)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LLMError(f"extractor output did not match schema: {exc}") from exc

    facts: list[ExtractedFact] = []
    errors = []
    for item in items:
        try:
            subject, subject_kind, subject_warning = _parse_fact_slot(item, "subject")
            relation, relation_kind, relation_warning = _parse_fact_slot(item, "relation")
            obj, object_kind, object_warning = _parse_fact_slot(item, "object")
            note = str(item.get("note", "")).strip()
            warnings = [w for w in (subject_warning, relation_warning, object_warning) if w]
            if warnings:
                note = "; ".join([part for part in (note, *warnings) if part])
            facts.append(
                ExtractedFact(
                    subject=subject,
                    relation=relation,
                    object=obj,
                    confidence=float(item["confidence"]),
                    note=note,
                    subject_kind=subject_kind,
                    relation_kind=relation_kind,
                    object_kind=object_kind,
                )
            )
        except (KeyError, TypeError, ValueError, TermParseError) as exc:
            errors.append(f"malformed fact object {item!r}: {exc}")
            continue
    if not facts and errors:
        raise LLMError(errors[0])
    return facts


def _fact_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise TypeError("facts output must be an array or object")
    for key in ("facts", "items", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    raise KeyError("facts")


def _decode_first_json(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise json.JSONDecodeError("empty provider output", raw, 0)
    decoder = json.JSONDecoder()
    try:
        return decoder.raw_decode(text)[0]
    except json.JSONDecodeError:
        starts = [idx for idx in (text.find("["), text.find("{")) if idx >= 0]
        if not starts:
            raise
        return decoder.raw_decode(text[min(starts):])[0]


def _parse_fact_slot(item: dict[str, Any], field: str) -> tuple[str, str, str]:
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
        try:
            term = parse_term(value)
            if not _is_ground_term(term):
                raise TermParseError(f"{field} structural term must be ground")
        except TermParseError as exc:
            return value, "string", f"{field} marked term but stored as string: {exc}"
    return value, kind, ""


def _is_ground_term(term: Term) -> bool:
    if isinstance(term, Var):
        return False
    if isinstance(term, Compound):
        return all(_is_ground_term(arg) for arg in term.args)
    return True
