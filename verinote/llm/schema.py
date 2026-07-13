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
from verinote.prompts import default_prompt_text

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

EXTRACTION_SYSTEM = default_prompt_text("extraction")


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
    return default_prompt_text("query-translation").replace("{qid}", str(qid))


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


QUERY_INTENT_SYSTEM = default_prompt_text("query-intent")


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
            note = _parse_fact_note(item)
            warnings = [w for w in (subject_warning, relation_warning, object_warning) if w]
            if warnings:
                note = "; ".join([part for part in (note, *warnings) if part])
            facts.append(
                ExtractedFact(
                    subject=subject,
                    relation=relation,
                    object=obj,
                    confidence=_parse_fact_confidence(item["confidence"]),
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


def _parse_fact_note(item: dict[str, Any]) -> str:
    """`note` is declared `{"type": "string"}`; a missing/null note means "no note"."""
    raw = item.get("note")
    if raw is None:
        return ""
    return _require_schema_string(raw, "note")


def _parse_fact_confidence(raw: Any) -> float:
    """`confidence` is declared `{"type": "number", "minimum": 0, "maximum": 1}`.

    Numeric strings stay tolerated -- prompt-only providers emit `"0.9"` often
    enough -- but booleans and out-of-range values are schema violations, not
    facts to be scored. `float(True)` would otherwise land as confidence 1.0.
    """
    if isinstance(raw, bool):
        raise TypeError("confidence must be a number, got bool")
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {value}")
    return value


def _require_schema_string(raw: Any, label: str) -> str:
    """Enforce the `{"type": "string"}` the slot schema declares.

    Coercing with `str()` instead would smuggle off-schema JSON (lists, objects,
    null) into the review queue as plausible-looking text -- `["Widget", "Alpha"]`
    would be stored as the literal `"['Widget', 'Alpha']"`, and `null` as `"None"`.
    """
    if not isinstance(raw, str):
        raise TypeError(f"{label} must be a string, got {type(raw).__name__}")
    return raw.strip()


def _parse_fact_slot(item: dict[str, Any], field: str) -> tuple[str, str, str]:
    raw = item[field]
    if isinstance(raw, str):
        # Prompt-only providers routinely emit a bare string for a plain slot;
        # per the fact-storage boundary that is a StringLit, never a compound.
        value = raw.strip()
        kind = "string"
    elif isinstance(raw, dict):
        kind = _require_schema_string(raw["kind"], f"{field}.kind")
        value = _require_schema_string(raw["value"], f"{field}.value")
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
