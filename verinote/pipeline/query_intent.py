# SPDX-License-Identifier: MPL-2.0
"""Structured query intent objects and deterministic intent parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import re
from typing import Any

from verinote.llm.base import LLMError


class QueryIntentKind(StrEnum):
    LOOKUP_OBJECT = "lookup_object"
    LOOKUP_SUBJECT = "lookup_subject"
    LOOKUP_RELATION = "lookup_relation"
    DISCOVER_ENTITY_RELATIONS = "discover_entity_relations"
    COUNT = "count"
    COMPARE_TYPED_VALUE = "compare_typed_value"
    UNKNOWN_OR_UNSUPPORTED = "unknown_or_unsupported"


@dataclass(frozen=True)
class IntentTarget:
    """A normalized query target without execution-language rendering."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"entity", "relation", "value", "typed_value"}:
            raise ValueError(f"unsupported target kind: {self.kind}")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("target value must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip())


@dataclass(frozen=True)
class QueryIntent:
    """Internal structured representation of a natural-language question."""

    kind: QueryIntentKind
    subject: IntentTarget | None = None
    relation: IntentTarget | None = None
    object: IntentTarget | None = None
    relation_candidates: tuple[str, ...] = field(default_factory=tuple)
    operator: str | None = None
    value_type: str | None = None
    value: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, QueryIntentKind):
            object.__setattr__(self, "kind", QueryIntentKind(self.kind))
        if not isinstance(self.relation_candidates, tuple):
            raise ValueError("relation_candidates must be a tuple")
        object.__setattr__(
            self,
            "relation_candidates",
            tuple(_clean_required_string(item, "relation candidate") for item in self.relation_candidates),
        )
        if self.operator is not None:
            object.__setattr__(self, "operator", _clean_required_string(self.operator, "operator"))
        if self.value_type is not None:
            object.__setattr__(self, "value_type", _clean_required_string(self.value_type, "value_type"))
        if self.value is not None:
            object.__setattr__(self, "value", _clean_required_string(self.value, "value"))
        if self.reason is not None:
            object.__setattr__(self, "reason", _clean_required_string(self.reason, "reason"))
        self._validate_combination()

    def _validate_combination(self) -> None:
        kind = self.kind
        has_relation = self.relation is not None or bool(self.relation_candidates)
        if kind == QueryIntentKind.LOOKUP_OBJECT:
            if self.subject is None or not has_relation or self.object is not None:
                raise ValueError("lookup_object requires subject and relation, and no object")
            self._require_target_kind("subject", self.subject, {"entity"})
            self._require_relation_field()
            self._forbid_compare_fields()
            self._forbid_reason()
        elif kind == QueryIntentKind.LOOKUP_SUBJECT:
            if self.subject is not None or not has_relation or self.object is None:
                raise ValueError("lookup_subject requires relation and object, and no subject")
            self._require_relation_field()
            self._require_target_kind("object", self.object, {"entity", "value", "typed_value"})
            self._forbid_compare_fields()
            self._forbid_reason()
        elif kind == QueryIntentKind.LOOKUP_RELATION:
            if self.relation is not None or self.relation_candidates:
                raise ValueError("lookup_relation does not accept a relation")
            if self.subject is None and self.object is None:
                raise ValueError("lookup_relation requires subject or object")
            self._require_optional_lookup_endpoint("subject", self.subject)
            self._require_optional_lookup_endpoint("object", self.object)
            self._forbid_compare_fields()
            self._forbid_reason()
        elif kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS:
            if self.subject is None or self.object is not None:
                raise ValueError(
                    "discover_entity_relations requires subject and no object"
                )
            if self.relation is not None and self.relation_candidates:
                raise ValueError(
                    "discover_entity_relations accepts relation or relation_candidates, not both"
                )
            self._require_target_kind("subject", self.subject, {"entity"})
            self._require_relation_field()
            self._forbid_compare_fields()
            self._forbid_reason()
        elif kind == QueryIntentKind.COUNT:
            if self.subject is None and self.object is None and not has_relation:
                raise ValueError("count requires at least one target or relation")
            self._require_optional_lookup_endpoint("subject", self.subject)
            if self.relation is not None:
                self._require_target_kind("relation", self.relation, {"relation"})
            self._require_optional_lookup_endpoint("object", self.object)
            self._forbid_compare_fields()
            self._forbid_reason()
        elif kind == QueryIntentKind.COMPARE_TYPED_VALUE:
            if (
                self.subject is None
                or not has_relation
                or self.operator is None
                or self.value_type is None
                or self.value is None
            ):
                raise ValueError(
                    "compare_typed_value requires subject, relation, operator, value_type, and value"
                )
            self._require_target_kind("subject", self.subject, {"entity"})
            self._require_relation_field()
            if self.operator not in {"=", "!=", "<", "<=", ">", ">="}:
                raise ValueError("compare_typed_value operator is invalid")
            if self.value_type not in {"date", "number", "amount", "ordinal"}:
                raise ValueError("compare_typed_value value_type is invalid")
            if self.object is not None:
                self._require_target_kind("object", self.object, {"typed_value", "value"})
            self._forbid_reason()
        elif kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED:
            if not self.reason:
                raise ValueError("unknown_or_unsupported requires a reason")
            if any(
                item is not None
                for item in (
                    self.subject,
                    self.relation,
                    self.object,
                    self.operator,
                    self.value_type,
                    self.value,
                )
            ) or self.relation_candidates:
                raise ValueError("unknown_or_unsupported accepts only kind and reason")

    def _forbid_compare_fields(self) -> None:
        if self.operator is not None or self.value_type is not None or self.value is not None:
            raise ValueError(f"{self.kind.value} does not accept comparison fields")

    def _forbid_reason(self) -> None:
        if self.reason is not None:
            raise ValueError(f"{self.kind.value} does not accept reason")

    def _require_relation_field(self) -> None:
        if self.relation is not None:
            self._require_target_kind("relation", self.relation, {"relation"})

    def _require_optional_lookup_endpoint(self, field_name: str, target: IntentTarget | None) -> None:
        if target is not None:
            self._require_target_kind(field_name, target, {"entity", "value", "typed_value"})

    def _require_target_kind(
        self, field_name: str, target: IntentTarget | None, allowed: set[str]
    ) -> None:
        if target is None:
            return
        if target.kind not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise ValueError(f"{self.kind.value} {field_name} must be {allowed_text}")


_ROLE_TITLE_QUESTION = re.compile(
    r'["“”\']?(?P<person>[^"“”\'?？\n]{1,80}?)["“”\']?\s*'
    r"(?:의|에\s*대한)\s*(?P<label>역할|직책|직위)"
)
_ENGLISH_ROLE_TITLE_QUESTION = re.compile(
    r"^\s*(?:what\s+is|what\s+was|find|show)\s+"
    r"(?:the\s+)?(?P<person>[A-Z][^?]{0,80}?)"
    r"(?:'s|\s+)?\s+(?P<label>role|title|position)\s*\??\s*$",
    re.IGNORECASE,
)
_ENGLISH_ENTITY_RELATION_DISCOVERY_QUESTION = re.compile(
    r"^\s*(?i:how\s+is|how\s+was)\s+"
    r"(?P<entity>[A-Z][^?]{0,80}?)\s+related\s*\??\s*$|"
    r"^\s*(?i:which\s+relation\s+connects)\s+"
    r"(?P<connected_entity>[A-Z][^?]{0,80}?)\s+to\s+other\s+facts\s*\??\s*$",
)
_ENGLISH_ENTITY_DIRECT_RELATION_DISCOVERY_QUESTION = re.compile(
    r"^\s*(?i:what\s+does)\s+(?P<entity>[A-Z][^?]{0,80}?)\s+"
    r"(?P<relation>(?i:provide|provides|offer|offers|connect|connects|relate|relates))\s*\?\s*$"
)
_KOREAN_ENTITY_RELATION_DISCOVERY_QUESTION = re.compile(
    r'["“”\']?(?P<entity>[^"“”\'?？\n]{1,80}?)["“”\']?\s*'
    r"(?:는|은|이|가)\s*어떤\s*관계(?:인가|입니까|야)?\s*\??\s*$"
)
_KOREAN_ENTITY_DIRECT_RELATION_DISCOVERY_QUESTION = re.compile(
    r'["“”\']?(?P<entity>[^"“”\'?？\n]{1,80}?)["“”\']?\s*'
    r"(?:는|은|이|가)\s*(?P<relation>제공)하는\s*것(?:은|이|인가|입니까)?\s*\??\s*$"
)
KOREAN_ROLE_RELATION_CANDIDATES = ("역할", "직책", "직위")
ENGLISH_ROLE_RELATION_CANDIDATES = ("role", "title", "position", "has_role")
KOREAN_PROVIDE_RELATION_CANDIDATES = (
    "제공",
    "제공기능",
    "제공 기능",
    "제공서비스",
    "제공 서비스",
    "제공요소",
    "제공 요소",
)
_GENERIC_ENTITY_ANCHORS = {
    "anything",
    "it",
    "something",
    "that",
    "this",
}


def deterministic_query_intent(question: str) -> QueryIntent:
    """Return a structured intent for deterministic synthetic question shapes."""
    text = question.strip()
    match = _ROLE_TITLE_QUESTION.search(text)
    if match:
        person = match.group("person").strip()
        if person:
            return QueryIntent(
                kind=QueryIntentKind.LOOKUP_OBJECT,
                subject=IntentTarget("entity", person),
                relation_candidates=KOREAN_ROLE_RELATION_CANDIDATES,
            )

    match = _ENGLISH_ROLE_TITLE_QUESTION.match(text)
    if match:
        person = match.group("person").strip()
        if person:
            return QueryIntent(
                kind=QueryIntentKind.LOOKUP_OBJECT,
                subject=IntentTarget("entity", person),
                relation_candidates=ENGLISH_ROLE_RELATION_CANDIDATES,
            )

    match = _ENGLISH_ENTITY_RELATION_DISCOVERY_QUESTION.match(text)
    if match:
        entity = (match.group("entity") or match.group("connected_entity")).strip()
        if entity and not _is_generic_entity_anchor(entity):
            return QueryIntent(
                kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
                subject=IntentTarget("entity", entity),
            )

    match = _ENGLISH_ENTITY_DIRECT_RELATION_DISCOVERY_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        relation = match.group("relation").strip()
        if entity and relation and not _is_generic_entity_anchor(entity):
            return QueryIntent(
                kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
                subject=IntentTarget("entity", entity),
                relation=IntentTarget("relation", relation),
            )

    match = _KOREAN_ENTITY_DIRECT_RELATION_DISCOVERY_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        if entity:
            return QueryIntent(
                kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
                subject=IntentTarget("entity", entity),
                relation_candidates=KOREAN_PROVIDE_RELATION_CANDIDATES,
            )

    match = _KOREAN_ENTITY_RELATION_DISCOVERY_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        if entity:
            return QueryIntent(
                kind=QueryIntentKind.DISCOVER_ENTITY_RELATIONS,
                subject=IntentTarget("entity", entity),
            )

    return QueryIntent(
        kind=QueryIntentKind.UNKNOWN_OR_UNSUPPORTED,
        reason="unsupported deterministic query shape",
    )


def _is_generic_entity_anchor(value: str) -> bool:
    return value.strip().casefold() in _GENERIC_ENTITY_ANCHORS


QUERY_INTENT_FIELDS = (
    "kind",
    "subject",
    "relation",
    "object",
    "relation_candidates",
    "operator",
    "value_type",
    "value",
    "reason",
)


def parse_query_intent(raw: str | dict[str, Any]) -> QueryIntent:
    """Parse constrained JSON provider output into an internal query intent."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise LLMError(f"query intent output was not JSON: {exc}") from exc
    try:
        return _parse_query_intent_object(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMError(f"query intent output did not match schema: {exc}") from exc


def _parse_query_intent_object(data: Any) -> QueryIntent:
    if not isinstance(data, dict):
        raise TypeError("query intent output must be an object")
    allowed = set(QUERY_INTENT_FIELDS)
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"unexpected fields: {', '.join(sorted(extra))}")
    missing = allowed - set(data)
    if missing:
        raise KeyError(", ".join(sorted(missing)))
    raw_kind = data["kind"]
    if not isinstance(raw_kind, str):
        raise TypeError("kind must be a string")
    try:
        kind = QueryIntentKind(raw_kind)
    except ValueError as exc:
        raise ValueError(f"unknown query intent kind: {raw_kind}") from exc
    return QueryIntent(
        kind=kind,
        subject=_parse_intent_target(data, "subject"),
        relation=_parse_intent_target(data, "relation"),
        object=_parse_intent_target(data, "object"),
        relation_candidates=_parse_relation_candidates(data),
        operator=_parse_optional_string(data, "operator"),
        value_type=_parse_optional_string(data, "value_type"),
        value=_parse_optional_string(data, "value"),
        reason=_parse_optional_string(data, "reason"),
    )


def _parse_intent_target(data: dict[str, Any], field_name: str) -> IntentTarget | None:
    raw = data[field_name]
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError(f"{field_name} must be an object or null")
    extra = set(raw) - {"kind", "value"}
    if extra:
        raise ValueError(f"{field_name} has unexpected fields: {', '.join(sorted(extra))}")
    if set(raw) != {"kind", "value"}:
        missing = sorted({"kind", "value"} - set(raw))
        raise KeyError(f"{field_name}.{missing[0]}")
    if not isinstance(raw["kind"], str):
        raise TypeError(f"{field_name}.kind must be a string")
    if not isinstance(raw["value"], str):
        raise TypeError(f"{field_name}.value must be a string")
    return IntentTarget(raw["kind"], raw["value"])


def _parse_relation_candidates(data: dict[str, Any]) -> tuple[str, ...]:
    raw = data["relation_candidates"]
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError("relation_candidates must be an array or null")
    if not all(isinstance(item, str) for item in raw):
        raise TypeError("relation_candidates items must be strings")
    return tuple(raw)


def _parse_optional_string(data: dict[str, Any], field_name: str) -> str | None:
    value = data[field_name]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or null")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _clean_required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text
