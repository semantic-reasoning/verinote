# SPDX-License-Identifier: MPL-2.0
"""Structured query intent objects and deterministic intent parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import re
from typing import Any

from verinote.llm.base import LLMError
from verinote.llm.schema import QUERY_INTENT_SCHEMA


def _schema_domain(field_name: str) -> frozenset[str] | None:
    """The non-null values QUERY_INTENT_SCHEMA's enum admits for one field.

    None when the schema pins no domain: `value` and `reason` carry `minLength: 1`
    and no enum, so every non-blank string is on-schema for them.
    """
    enum = QUERY_INTENT_SCHEMA["properties"][field_name].get("enum")
    if enum is None:
        return None
    return frozenset(value for value in enum if value is not None)


# The schema's nullable string properties. Each is typed `["string", "null"]` and
# each is still listed in `required` (OpenAI strict mode), so any of them can
# legitimately arrive as null.
_NULLABLE_STRING_FIELDS = ("operator", "value_type", "value", "reason")

# Read off the schema rather than restated here: the schema is the contract every
# adapter hands the provider, so a second hand-maintained copy of these enums can
# only drift out of it -- and either half of that drift is a bug (rejecting
# on-schema output, or accepting output no strict-mode provider could send).
QUERY_INTENT_COMPARISON_DOMAINS: dict[str, frozenset[str]] = {
    field_name: domain
    for field_name in _NULLABLE_STRING_FIELDS
    if (domain := _schema_domain(field_name)) is not None
}

# Blank means null only where the schema pins no domain. A prompt-only provider
# spells the null it is forced to emit as "", so `reason: ""` has to read as
# absent (issue #237) -- but "" is in no enum, so on an enum-constrained field it
# is an off-schema *value*, not an absent one, and `_validate_schema_domains`
# must get to see it. Derived from the schema so the rule cannot drift out of the
# contract: give `value` an enum tomorrow and it stops taking blank as null here,
# with no name list to remember to update.
QUERY_INTENT_BLANK_NULLABLE_FIELDS: frozenset[str] = frozenset(
    field_name
    for field_name in _NULLABLE_STRING_FIELDS
    if _schema_domain(field_name) is None
)


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
    """Internal structured representation of a natural-language question.

    `reason` and the comparison fields (operator/value_type/value) are advisory:
    any kind may carry them, and only the kind that consumes one requires it.
    `unknown_or_unsupported` requires `reason` and accepts nothing else;
    `compare_typed_value` requires all three comparison fields. Advisory means
    "ignored", never "unchecked" -- a non-null operator or value_type is held to
    QUERY_INTENT_SCHEMA's enum on every kind (`_validate_schema_domains`), so the
    validator never accepts what the schema forbids.

    QUERY_INTENT_SCHEMA must list every property as required -- OpenAI strict
    mode forbids conditional requirements -- and its `operator` enum admits "=".
    So a model that answers "Who is the CEO of Acme?" with `lookup_object` +
    `operator: "="`, or that fills in a `reason` for a question it classified
    correctly, is emitting schema-legal output. Rejecting it discarded a correct
    intent over fields nothing outside this module reads (the planner does not
    even branch on `compare_typed_value`), and that is what failed every
    translation in issue #237.

    Still rejected: an off-schema *value* (`value_type="duration"`, on any kind)
    and a wrong *shape* (a `lookup_object` with no subject). Those are outside the
    contract the provider was handed; a schema-legal field nobody reads is not.
    """

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
        # A blank nullable string is an absent one -- but only where the schema
        # pins no domain for the field. All four are nullable yet still listed in
        # `required` (OpenAI strict mode), and prompt-only providers do not
        # enforce `minLength: 1`, so a model told to "leave reason null" routinely
        # emits "" instead; treating that as a hard error would kill a correctly
        # classified intent, the same failure as #237. On `operator`/`value_type`
        # the schema's enum settles it the other way: "" is not in the enum, so it
        # is an off-schema value that `_validate_schema_domains` must reject
        # rather than an absent one to normalise away.
        for field_name in _NULLABLE_STRING_FIELDS:
            current = getattr(self, field_name)
            if current is not None:
                object.__setattr__(self, field_name, _clean_optional_string(current, field_name))
        self._validate_schema_domains()
        self._validate_combination()

    def _validate_schema_domains(self) -> None:
        """Hold every non-null field to the schema's enum, on every kind.

        Tolerating a stray comparison field means ignoring it, not exempting it
        from the contract: `operator: "="` on a lookup_object is schema-legal and
        harmless, while `operator: "contains"` is off-schema everywhere. Checking
        only inside the compare_typed_value branch would leave the validator
        accepting, on the other six kinds, output QUERY_INTENT_SCHEMA forbids.
        """
        for field_name, allowed in QUERY_INTENT_COMPARISON_DOMAINS.items():
            current = getattr(self, field_name)
            if current is not None and current not in allowed:
                allowed_text = ", ".join(sorted(allowed))
                raise ValueError(f"{field_name} must be one of {allowed_text}, got {current!r}")

    def _validate_combination(self) -> None:
        kind = self.kind
        has_relation = self.relation is not None or bool(self.relation_candidates)
        if kind == QueryIntentKind.LOOKUP_OBJECT:
            if self.subject is None or not has_relation or self.object is not None:
                raise ValueError("lookup_object requires subject and relation, and no object")
            self._require_target_kind("subject", self.subject, {"entity"})
            self._require_relation_field()
        elif kind == QueryIntentKind.LOOKUP_SUBJECT:
            if self.subject is not None or not has_relation or self.object is None:
                raise ValueError("lookup_subject requires relation and object, and no subject")
            self._require_relation_field()
            self._require_target_kind("object", self.object, {"entity", "value", "typed_value"})
        elif kind == QueryIntentKind.LOOKUP_RELATION:
            if self.relation is not None or self.relation_candidates:
                raise ValueError("lookup_relation does not accept a relation")
            if self.subject is None and self.object is None:
                raise ValueError("lookup_relation requires subject or object")
            self._require_optional_lookup_endpoint("subject", self.subject)
            self._require_optional_lookup_endpoint("object", self.object)
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
        elif kind == QueryIntentKind.COUNT:
            if self.subject is None and self.object is None and not has_relation:
                raise ValueError("count requires at least one target or relation")
            self._require_optional_lookup_endpoint("subject", self.subject)
            if self.relation is not None:
                self._require_target_kind("relation", self.relation, {"relation"})
            self._require_optional_lookup_endpoint("object", self.object)
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
            # operator/value_type values are checked in _validate_schema_domains,
            # which applies to every kind; this branch only adds that compare
            # cannot do without them.
            if self.object is not None:
                self._require_target_kind("object", self.object, {"typed_value", "value"})
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
_KOREAN_ATTRIBUTE_QUESTION = re.compile(
    r'^\s*["“”\']?(?P<entity>[^"“”\'?？\n]{1,100}?)["“”\']?\s*'
    r"(?:의|에\s*대한)\s*(?P<label>[^?？\n]{1,80})\s*[?？]?\s*$"
)
_ENGLISH_POSSESSIVE_ATTRIBUTE_QUESTION = re.compile(
    r"^\s*(?i:what\s+is|what\s+was|find|show)\s+(?:the\s+)?"
    r"(?P<entity>[A-Z][^?]{0,100}?)'s\s+"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _-]{0,40})\s*\??\s*$"
)
_ENGLISH_OF_ATTRIBUTE_QUESTION = re.compile(
    r"^\s*(?i:what\s+is|what\s+was|find|show)\s+(?:the\s+)?"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _-]{0,40})\s+of\s+"
    r"(?P<entity>[A-Z][^?]{0,100}?)\s*\??\s*$"
)
KOREAN_ROLE_RELATION_CANDIDATES = ("역할", "직책", "직위")
ENGLISH_ROLE_RELATION_CANDIDATES = ("role", "title", "position", "has_role")
PURPOSE_RELATION_CANDIDATES = ("목적", "목표", "purpose", "objective", "goal")
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

    match = _KOREAN_ATTRIBUTE_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        raw_label = match.group("label")
        label = _clean_korean_attribute_label(raw_label)
        if entity and label and _looks_like_korean_attribute_question(raw_label, text):
            return QueryIntent(
                kind=QueryIntentKind.LOOKUP_OBJECT,
                subject=IntentTarget("entity", entity),
                relation_candidates=_attribute_relation_candidates(label),
            )

    match = _ENGLISH_POSSESSIVE_ATTRIBUTE_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        label = _clean_english_attribute_label(match.group("label"))
        if entity and label and not _is_generic_entity_anchor(entity):
            return QueryIntent(
                kind=QueryIntentKind.LOOKUP_OBJECT,
                subject=IntentTarget("entity", entity),
                relation_candidates=_attribute_relation_candidates(label),
            )

    match = _ENGLISH_OF_ATTRIBUTE_QUESTION.match(text)
    if match:
        entity = match.group("entity").strip()
        label = _clean_english_attribute_label(match.group("label"))
        if entity and label and not _is_generic_entity_anchor(entity):
            return QueryIntent(
                kind=QueryIntentKind.LOOKUP_OBJECT,
                subject=IntentTarget("entity", entity),
                relation_candidates=_attribute_relation_candidates(label),
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
                relation=IntentTarget("relation", "제공"),
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


def _clean_korean_attribute_label(value: str) -> str:
    label = " ".join(value.strip().split())
    label = re.sub(
        r"\s*(?:무엇(?:인가|입니까)?|뭐(?:야|입니까)?|어떤\s*것(?:인가|입니까)?|인가|입니까)\s*$",
        "",
        label,
    ).strip()
    label = re.sub(r"(?:은|는|이|가)\s*$", "", label).strip()
    return label


def _looks_like_korean_attribute_question(raw_label: str, question: str) -> bool:
    tail = raw_label.strip()
    if question.rstrip().endswith(("?", "？")):
        return True
    return bool(
        re.search(
            r"(?:은|는|이|가|무엇(?:인가|입니까)?|뭐(?:야|입니까)?|"
            r"어떤\s*것(?:인가|입니까)?|인가|입니까)\s*$",
            tail,
        )
    )


def _clean_english_attribute_label(value: str) -> str:
    label = " ".join(value.strip().split())
    return label.removeprefix("the ").strip()


def _attribute_relation_candidates(label: str) -> tuple[str, ...]:
    normalized = label.strip()
    folded = normalized.casefold()
    if folded in {"목적", "목표", "프로젝트 목적", "사업 목적", "purpose", "objective", "goal"}:
        return PURPOSE_RELATION_CANDIDATES
    return (normalized,)


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
    # Only `kind` is load-bearing. Every other field is declared nullable in
    # QUERY_INTENT_SCHEMA, so an omitted key and an explicit null say the same
    # thing -- and prompt-only providers (claude_cli renders the schema as text
    # rather than constraining decoding) drop null keys routinely.
    if "kind" not in data:
        raise KeyError("kind")
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
    raw = data.get(field_name)
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
    raw = data.get("relation_candidates")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError("relation_candidates must be an array or null")
    if not all(isinstance(item, str) for item in raw):
        raise TypeError("relation_candidates items must be strings")
    return tuple(raw)


def _parse_optional_string(data: dict[str, Any], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or null")
    return _clean_optional_string(value, field_name)


def _clean_optional_string(value: str, field_name: str) -> str | None:
    """Trim a nullable string field, mapping blank to None off the enum fields.

    Blank is how a prompt-only provider spells null in a key the schema forces it
    to emit, so on a field the schema leaves open (`value`, `reason`) it means
    "absent" rather than "invalid". Where the schema pins an enum it means neither:
    "" is not one of the admitted values, so it is returned as-is for
    `_validate_schema_domains` to reject, the same as `operator="contains"`. A
    wrong *value* was always a violation; the enum fields simply have no spelling
    of null other than null.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text and field_name in QUERY_INTENT_BLANK_NULLABLE_FIELDS:
        return None
    return text


def _clean_required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text
