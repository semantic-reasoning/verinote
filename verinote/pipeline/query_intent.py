# SPDX-License-Identifier: MPL-2.0
"""Structured query intent objects and deterministic intent parsing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
import json
import re
from typing import Any

from verinote.llm.base import LLMError
from verinote.llm.schema import QUERY_INTENT_SCHEMA
from verinote.text import nfc


# Every derivation below takes its schema as an argument rather than reading
# QUERY_INTENT_SCHEMA directly. That is what makes the derivation testable: fed a
# synthetic schema carrying a property the module has never heard of, a real
# derivation answers with that property and a hand-written name list cannot. The
# module constants are these functions applied to the real contract.
def _nullable_string_fields(schema: dict[str, Any]) -> tuple[str, ...]:
    """The schema's nullable string properties, in schema order.

    A property qualifies when its declared type is exactly `["string", "null"]`.
    Testing `"null" in type` instead would sweep in `relation_candidates`
    (`["array", "null"]`) and the target properties (`["object", "null"]`), whose
    values are not strings and must not be trimmed or blank-normalised as if they
    were. Each qualifying property is also listed in `required` (OpenAI strict
    mode), so any of them can legitimately arrive as null.
    """
    return tuple(
        field_name
        for field_name, spec in schema["properties"].items()
        if isinstance(spec.get("type"), list) and set(spec["type"]) == {"string", "null"}
    )


def _schema_domain(schema: dict[str, Any], field_name: str) -> frozenset[str] | None:
    """The non-null values the schema's enum admits for one field.

    None when the schema pins no domain: `value` and `reason` carry `minLength: 1`
    and no enum, so every non-blank string is on-schema for them.
    """
    enum = schema["properties"][field_name].get("enum")
    if enum is None:
        return None
    return frozenset(value for value in enum if value is not None)


def _comparison_domains(schema: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Every nullable string property the schema constrains to an enum.

    Read off the schema rather than restated here: the schema is the contract
    every adapter hands the provider, so a second hand-maintained copy of these
    enums can only drift out of it -- and either half of that drift is a bug
    (rejecting on-schema output, or accepting output no strict-mode provider
    could send).
    """
    return {
        field_name: domain
        for field_name in _nullable_string_fields(schema)
        if (domain := _schema_domain(schema, field_name)) is not None
    }


def _blank_nullable_fields(schema: dict[str, Any]) -> frozenset[str]:
    """The nullable string properties on which blank reads as null.

    Blank means null only where the schema pins no domain. A prompt-only provider
    spells the null it is forced to emit as "", so `reason: ""` has to read as
    absent (issue #237) -- but "" is in no enum, so on an enum-constrained field
    it is an off-schema *value*, not an absent one, and `_validate_schema_domains`
    must get to see it. Add an enum to `value` tomorrow and it stops taking blank
    as null here; add a whole new nullable string property and it starts,
    with no name list to remember to update either way.
    """
    return frozenset(
        field_name
        for field_name in _nullable_string_fields(schema)
        if _schema_domain(schema, field_name) is None
    )


_NULLABLE_STRING_FIELDS = _nullable_string_fields(QUERY_INTENT_SCHEMA)

QUERY_INTENT_COMPARISON_DOMAINS: dict[str, frozenset[str]] = _comparison_domains(
    QUERY_INTENT_SCHEMA
)

QUERY_INTENT_BLANK_NULLABLE_FIELDS: frozenset[str] = _blank_nullable_fields(
    QUERY_INTENT_SCHEMA
)


class QueryIntentKind(StrEnum):
    LOOKUP_OBJECT = "lookup_object"
    LOOKUP_SUBJECT = "lookup_subject"
    LOOKUP_RELATION = "lookup_relation"
    DISCOVER_ENTITY_RELATIONS = "discover_entity_relations"
    COMPARE_TYPED_VALUE = "compare_typed_value"
    CONJUNCTIVE_LOOKUP = "conjunctive_lookup"
    CONJUNCTIVE_FILTER = "conjunctive_filter"
    CONJUNCTIVE_THREE_HOP_LOOKUP = "conjunctive_three_hop_lookup"
    UNKNOWN_OR_UNSUPPORTED = "unknown_or_unsupported"


INTENT_TARGET_KINDS = frozenset({"entity", "relation", "value", "typed_value"})
"""The target kinds `IntentTarget` admits.

Named rather than written inline because the schema-shape check below reads it
too: a nullable object property whose `kind` enum reaches outside this set is
schema-legal output the parser would refuse, so the dispatch has to compare
against the same set `__post_init__` enforces rather than a second copy of it.
"""


@dataclass(frozen=True)
class IntentTarget:
    """A normalized query target without execution-language rendering."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in INTENT_TARGET_KINDS:
            raise ValueError(f"unsupported target kind: {self.kind}")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("target value must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip())


_CONJUNCTIVE_VARIABLE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ConjunctiveEndpoint:
    """One constant entity or named variable in a bounded conjunctive hop."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"entity", "var"}:
            raise ValueError("conjunctive endpoint kind must be entity or var")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("conjunctive endpoint value must be a non-empty string")
        value = self.value.strip()
        if self.kind == "var" and not _CONJUNCTIVE_VARIABLE.fullmatch(value):
            raise ValueError("conjunctive variable must be a Datalog variable name")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class ConjunctiveHop:
    """One relation/3 atom for the narrow two-hop lookup contract."""

    subject: ConjunctiveEndpoint
    relation: IntentTarget
    object: ConjunctiveEndpoint

    def __post_init__(self) -> None:
        if self.relation.kind != "relation":
            raise ValueError("conjunctive hop relation must be relation")


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
    intent over advisory fields the planner does not consume, and that is what
    failed every translation in issue #237. A compare intent does consume its
    threshold fields and rejects an object constraint rather than ignoring it.

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
    hops: tuple[ConjunctiveHop, ...] = field(default_factory=tuple)
    conditions: tuple[ConjunctiveHop, ...] = field(default_factory=tuple)
    chain_hops: tuple[ConjunctiveHop, ...] = field(default_factory=tuple)
    answer_var: str | None = None

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
        if not isinstance(self.hops, tuple):
            raise ValueError("hops must be a tuple")
        if not isinstance(self.conditions, tuple):
            raise ValueError("conditions must be a tuple")
        if not isinstance(self.chain_hops, tuple):
            raise ValueError("chain_hops must be a tuple")
        if self.answer_var is not None:
            object.__setattr__(self, "answer_var", _clean_optional_string(self.answer_var, "answer_var"))
        # A blank nullable string is an absent one -- but only where the schema
        # pins no domain for the field. Every schema property typed
        # `["string", "null"]` is normalised here, and each is still listed in
        # `required` (OpenAI strict mode); prompt-only providers do not enforce
        # `minLength: 1`, so a model told to "leave reason null" routinely emits
        # "" instead, and treating that as a hard error would kill a correctly
        # classified intent, the same failure as #237. On the enum-constrained
        # ones (`operator`/`value_type` today) the schema settles it the other
        # way: "" is in no enum, so it is an off-schema value that
        # `_validate_schema_domains` must reject rather than an absent one to
        # normalise away.
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
        if kind not in {
            QueryIntentKind.CONJUNCTIVE_LOOKUP,
            QueryIntentKind.CONJUNCTIVE_FILTER,
            QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP,
        } and (self.hops or self.conditions or self.chain_hops or self.answer_var is not None):
            raise ValueError(f"{kind.value} does not accept conjunctive fields")
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
            if self.object is not None:
                raise ValueError("compare_typed_value does not accept object")
        elif kind == QueryIntentKind.CONJUNCTIVE_LOOKUP:
            if any(
                item is not None
                for item in (self.subject, self.relation, self.object, self.operator, self.value_type, self.value)
            ) or self.relation_candidates or self.conditions or self.chain_hops:
                raise ValueError("conjunctive_lookup accepts only hops and answer_var")
            if len(self.hops) != 2 or not self.answer_var:
                raise ValueError("conjunctive_lookup requires exactly two hops and answer_var")
            first, second = self.hops
            if first.subject.kind != "entity" or first.object.kind != "var":
                raise ValueError("conjunctive_lookup first hop requires entity subject and variable object")
            if second.subject.kind != "var" or second.object.kind != "var":
                raise ValueError("conjunctive_lookup second hop requires variable endpoints")
            if second.subject.value != first.object.value:
                raise ValueError("conjunctive_lookup hops must share the intermediate variable")
            if second.object.value != self.answer_var:
                raise ValueError("conjunctive_lookup answer_var must be the second hop object")
        elif kind == QueryIntentKind.CONJUNCTIVE_FILTER:
            if any(
                item is not None
                for item in (self.subject, self.relation, self.object, self.operator, self.value_type, self.value)
            ) or self.relation_candidates or self.hops or self.chain_hops:
                raise ValueError("conjunctive_filter accepts only conditions and answer_var")
            if len(self.conditions) != 2 or not self.answer_var:
                raise ValueError("conjunctive_filter requires exactly two conditions and answer_var")
            if not _CONJUNCTIVE_VARIABLE.fullmatch(self.answer_var):
                raise ValueError("conjunctive_filter answer_var must be a Datalog variable name")
            for condition in self.conditions:
                endpoints = (condition.subject, condition.object)
                if any(
                    endpoint.kind == "var" and endpoint.value != self.answer_var
                    for endpoint in endpoints
                ):
                    raise ValueError("conjunctive_filter does not accept additional variables")
                if sum(endpoint.kind == "var" and endpoint.value == self.answer_var for endpoint in endpoints) != 1:
                    raise ValueError("each conjunctive_filter condition must contain answer_var exactly once")
                if any(endpoint.kind != "entity" for endpoint in endpoints if endpoint.value != self.answer_var):
                    raise ValueError("conjunctive_filter requires an entity anchor in each condition")
        elif kind == QueryIntentKind.CONJUNCTIVE_THREE_HOP_LOOKUP:
            if any(
                item is not None
                for item in (self.subject, self.relation, self.object, self.operator, self.value_type, self.value)
            ) or self.relation_candidates or self.hops or self.conditions:
                raise ValueError("conjunctive_three_hop_lookup accepts only chain_hops and answer_var")
            if len(self.chain_hops) != 3 or not self.answer_var:
                raise ValueError("conjunctive_three_hop_lookup requires exactly three chain_hops and answer_var")
            first, second, third = self.chain_hops
            if first.subject.kind != "entity" or first.object.kind != "var":
                raise ValueError("conjunctive_three_hop_lookup first hop requires entity subject and variable object")
            if second.subject.kind != "var" or second.object.kind != "var":
                raise ValueError("conjunctive_three_hop_lookup second hop requires variable endpoints")
            if third.subject.kind != "var" or third.object.kind != "var":
                raise ValueError("conjunctive_three_hop_lookup third hop requires variable endpoints")
            if second.subject.value != first.object.value:
                raise ValueError("conjunctive_three_hop_lookup first and second hops must share the intermediate variable")
            if third.subject.value != second.object.value:
                raise ValueError("conjunctive_three_hop_lookup second and third hops must share the intermediate variable")
            if len({first.object.value, second.object.value, self.answer_var}) != 3:
                raise ValueError("conjunctive_three_hop_lookup variables must be distinct")
            if third.object.value != self.answer_var:
                raise ValueError("conjunctive_three_hop_lookup answer_var must be the third hop object")
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
                relation_candidates=_korean_attribute_relation_candidates(raw_label),
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


_KOREAN_INTERROGATIVE_TAIL = (
    r"무엇(?:인가요?|입니까)?"
    r"|누구(?:인가요?|입니까|예요|이에요|야)?"
    r"|얼마(?:인가요?|입니까|예요|이에요|야)?"
    r"|어디(?:인가요?|입니까|예요|이에요|야)?"
    r"|언제(?:인가요?|입니까|예요|이에요|야)?"
    r"|뭐(?:인가요?|입니까|예요|야)?"
    r"|어떤\s*것(?:인가요?|입니까)?"
    r"|인가|입니까"
)
"""The interrogative tails `_clean_korean_attribute_label` strips.

Every alternative is an interrogative stem with an optional copula suffix,
except the two bare endings `인가`/`입니까` discussed below.
`_KOREAN_MEASURE_QUESTION_TAIL` carries the tails this alternation cannot
express, the ones with a counter noun or a conjugated predicate inside them
(`몇 살인가`, `얼마나 되나요`).

`누구` and `얼마` are how Korean asks for a person and an amount, and stripping
only `무엇` left `프로젝트A의 담당자는 누구인가?` asking for a relation literally
named `담당자는 누구` -- a label no schema can hold, so the planner built no
candidates and a question naming its relation exactly was answered UNVERIFIED.
`어디` and `언제` are the same defect for a place and a time.

This is the stems and suffix forms observed so far, not the whole interrogative
class: `무엇` and `어떤 것` take no `예요`/`야` here, and no stem takes a past
form, so `담당자는 누구였나요?` is still left unstripped -- untouched by this
rule rather than handled by it.

Every suffix this rule adds is reachable only bound to a stem. That is what
keeps it safe: admitting a stemless `요` or `야` alternative would cut a relation
named `개요` down to `개`, or `분야` to `분`. The two stemless alternatives,
`인가` and `입니까`, are HEAD's and are left exactly as they were -- deliberately
not widened to `인가요?`, which would have cut `샘플사업의 재인가요?` down to
`재`, the very hazard the stem-bound rule exists to avoid. They are also why
`샘플사업의 인가?`, naming a relation spelled like the copula, already loses its
whole label on the unchanged path.
"""

_KOREAN_ATTRIBUTE_LABEL_TAIL = re.compile(
    rf"\s*(?:{_KOREAN_INTERROGATIVE_TAIL})\s*$"
)
_KOREAN_ATTRIBUTE_LABEL_JOSA = re.compile(r"(?:은|는|이|가)\s*$")

_KOREAN_MEASURE_COUNTER = (
    r"살|명|개|건|년|개월|달|주|일|시간|분|초|번|회|차|가지|종류|종|"
    r"퍼센트|프로|원|점|위|권|장|쪽|편|배|층"
)
_KOREAN_MEASURE_PREDICATE = r"인가요?|입니까|이에요|이야|예요|야"
_KOREAN_MEASURE_QUESTION_TAIL = (
    rf"(?<![가-힣])몇\s*(?:(?P<counter>{_KOREAN_MEASURE_COUNTER}))?\s*(?:{_KOREAN_MEASURE_PREDICATE})?"
    r"|(?<![가-힣])얼마나(?:\s*[가-힣]{1,6}){0,2}"
)
"""The measure-question tails `_clean_korean_attribute_label` strips.

`샘플인물의 나이는 몇 살인가?` asked the schema for a relation named
`나이는 몇 살`. No schema holds that, so a question naming its relation exactly
was answered UNVERIFIED -- the same defect `누구`/`얼마` had, in a different
shape: a counter noun sits between the interrogative and the predicate, which
`_KOREAN_INTERROGATIVE_TAIL`'s stem-and-suffix alternatives cannot express. That
is why this is a separate pattern rather than two more stems there.

The two halves are bounded differently because they complement different word
classes. `몇` takes a counter noun, an enumerable class, so the counters are
listed. `얼마나` takes a conjugated predicate, which is open, so it takes a
bounded wildcard: Hangul syllables only, in at most two runs of at most six,
anchored to the end of the label. A run may end mid-word but may never cross a
space, so where the space falls decides: `얼마나 소요되겠습니까?` is one word of
seven and is stripped, two runs covering it between them, while
`얼마나 가나 다라마바사아자` is nine and is not: one run is spent on the first
word, leaving the second word's seven syllables to a single run of six. Twelve
syllables is the ceiling -- one word of twelve, or two words of six.

Both interrogatives carry `(?<![가-힣])`, so neither may follow a Hangul
syllable. `몇몇` is an ordinary Korean determiner, and without that guard its
second syllable matched the bare-`몇` form and cut `몇몇` down to `몇`. What the
guard buys for the bare `야`/`예요` predicates is narrower than safety: inside a
Hangul label they are reachable only bound to a word-initial `몇`, so this rule
cannot cut a word in half the way a stemless `야` would leave `분야` as `분` --
it takes whole words. The guard names Hangul only, so any non-Hangul character
before the interrogative -- punctuation as much as another script -- falls
outside that reasoning, and there the rule does leave a fragment:
`샘플대상의 가격-몇 개?` asks for `가격-`. A label whose tail really is `몇` +
counter + `야` still loses that phrase: `샘플대상의 최근 몇 주야?` asks only for
`최근`. That is the "several" case below, not a truncation.

The guard is on the interrogative, not on the counter, so `몇살인가?` is still
read; the spaces on either side of the counter are both optional, so
`몇 살 인가?` is read too. What falls outside is any label with no space before
the interrogative -- `나이는몇살인가?` and `가격은얼마나?` alike.

`몇` also means "several" non-interrogatively and nothing here settles which it
is: `샘플기간의 최근 몇 년?` loses its `몇 년` and asks only for `최근`. That is
an accepted cost, weighed against `최근 몇 년` being an implausible relation
name, not a case this handles.

Not covered, and stated rather than implied: a counter outside the list
(`몇 톤인가?`), a non-Hangul unit (`몇 %인가?`), an ordinal (`몇 번째인가?`), the
`-나` particle (`몇 개나 되나요?`). Past forms such as `몇 살이었나요?` are
untouched by this rule, the same gap `_KOREAN_INTERROGATIVE_TAIL` records for
`누구였나요?`.

The counter is captured as `counter` so the unit rule below can read the word
this strip discards -- the strip itself does not consult the group, and naming
it changed no substitution this pattern makes. On the `얼마나` branch the group
does not participate, so it reads back as None, which is the right answer:
`얼마나` names no unit.
"""

_KOREAN_ATTRIBUTE_LABEL_MEASURE_TAIL = re.compile(
    rf"\s*(?:{_KOREAN_MEASURE_QUESTION_TAIL})\s*$"
)

_MEASUREMENT_FAMILY = {
    "YEAR": "time", "MONTH": "time", "WEEK": "time", "DAY": "time",
    "HOUR": "time", "MINUTE": "time", "SECOND": "time",
    "PERCENT": "ratio", "TIMES": "ratio",
    "KRW": "money", "USD": "money", "JPY": "money", "EUR": "money",
}
"""The quantity each canonical unit measures.

Two units in one family measure the same thing, so asking in one and being
answered in the other is a mismatch a reader can act on. Two units in different
families are not comparable at all: `몇 원인가?` answered `2년` is a money
question answered with a duration, which is a mis-read question or a mis-stored
fact rather than a unit difference, and saying "no unit conversion is applied"
would be the wrong thing to tell that reader. So the cross-family case is
silent.
"""

_MEASUREMENT_UNIT_SPELLINGS = {
    "년": "YEAR", "연": "YEAR", "살": "YEAR", "세": "YEAR", "year": "YEAR", "years": "YEAR",
    "개월": "MONTH", "달": "MONTH", "month": "MONTH", "months": "MONTH",
    "주": "WEEK", "주일": "WEEK", "week": "WEEK", "weeks": "WEEK",
    "일": "DAY", "day": "DAY", "days": "DAY",
    "시간": "HOUR", "hour": "HOUR", "hours": "HOUR",
    "분": "MINUTE", "minute": "MINUTE", "minutes": "MINUTE",
    "초": "SECOND", "second": "SECOND", "seconds": "SECOND",
    "퍼센트": "PERCENT", "프로": "PERCENT", "%": "PERCENT", "percent": "PERCENT",
    "배": "TIMES",
    "원": "KRW", "won": "KRW",
    "달러": "USD", "dollar": "USD", "dollars": "USD",
    "엔": "JPY", "yen": "JPY",
    "유로": "EUR", "euro": "EUR",
}
"""Every spelling read as a unit, mapped to its canonical unit.

One table serves both sides of the comparison -- the counter a question asks in
and the unit a value states -- so a row added for one side is live on the other,
and a test whose asked counter is the row under test cannot pin that row: delete
the row and the question side goes silent too.

Absent on purpose, each for its own reason:

* bare `월`. `3월` is March, and `개월` is the counter for a month-count and is
  present. Nothing here reads `월` as a month of the year either, though: the
  row is absent, so a `6월` that really did mean six months states no unit this
  rule can see, and is silent for want of any reading rather than by a
  judgement between two.
* `일` is present, unlike `월`: `3일` is three days far more often than it is the
  third of the month. That is a judgement about which reading is commoner, not a
  guarantee that the other one is caught. `_CALENDAR_DATE` reaches a day of the
  month only when a digit month stands beside the day with nothing but
  whitespace between them, so `3월 15일` and `3월15일` are dates while
  `매월 15일`, `15일 마감`, `3월 중 15일`, `3월의 15일` and `3월 말 15일` are not,
  and all of those do state `일` -- the last three have the month in digits and
  are still missed, because a word between the two ends the match. See
  `korean_measure_unit_mismatch`.
* `개년`. It fired on `5개년 계획`, which is the name of a plan rather than a
  duration.
* `$`, `₩`, `€`. Every quantity here begins at a digit, and these precede their
  number, so no row in this table could reach them. `%` is read because it
  follows the number. That asymmetry is by construction; it is not an omission
  a row would restore.
"""

_UNIT_SUFFIX_MEMBERS = ("간", "가량", "정도", "쯤", "짜리")
"""The members of `_UNIT_SUFFIX`, named so a test can assert over the live set.

Every member is Hangul, and `_VALUE_MEASUREMENT_RELAXED` is dropped from that
pattern on exactly that ground. A test that restated the members as its own
literal would go on passing if a non-Hangul one were added here, which is the
case the argument does not survive: a digit member makes `3년2주` read as `년`
alone and a `몇 주인가?` against it answer "the verified value states 주".
"""

_UNIT_SUFFIX = r"(?:" + "|".join(_UNIT_SUFFIX_MEMBERS) + r")?"
"""The particles that may follow a unit and still leave it read as one.

`2년간` states two years. The set is closed, and the lookahead in
`_VALUE_MEASUREMENT` still applies after it, so `3일간의 일정` and `3주간격`
state no unit here: with the suffix taken, the next character is Hangul and the
lookahead refuses it; without it, `간` is Hangul and the lookahead refuses that.
"""

_VALUE_MEASUREMENT = re.compile(
    r"[0-9][0-9,.]*\s*[만억천조]?\s*(?P<unit>"
    + "|".join(re.escape(s) for s in _MEASUREMENT_UNIT_SPELLINGS)
    + r")" + _UNIT_SUFFIX + r"(?![가-힣0-9A-Za-z])"
)
"""One quantity stated inside a value: digits, at most one Korean magnitude
word, and a unit spelling.

`[만억천조]?` is one character, not a run, so `3만원` is read and `2천만원` is
not -- a number that stacks magnitudes states nothing this pattern can see.

Requiring the digits is the whole precision of this rule. Ordinary Korean prose
is full of syllables that are also unit spellings -- `지원`, `내년`, `일정`,
`분야` -- and read without a number in front of them they turn a caveat into
noise. The prose sweep in the tests is what measures that.

The alternation is built from the spellings table in the table's own order, and
what reads `3달러` as USD is the trailing lookahead plus backtracking rather
than that order: `달` is tried first and matches, the lookahead rejects it
because `러` is Hangul and so inside the lookahead's class, and the engine
backtracks into `달러`. Every prefix pair in the table today is extended by a
Hangul or Latin character, both of which that class covers, which is why
re-sorting the table by length changes no reading. A spelling extended by
punctuation would sit outside the class, and there the order would decide.
"""

_CALENDAR_DATE = re.compile(
    r"[0-9]{1,2}\s*월\s*[0-9]{1,2}\s*일"
    r"|(?<![0-9])[0-9]{2,4}\s*년\s*[0-9]{1,2}\s*월"
    r"|(?<![0-9])[0-9]{4}\s*년(?![0-9])"
    r"|(?<![0-9])[0-9]{2,4}\s*[-./]\s*[0-9]{1,2}\s*[-./]\s*[0-9]{1,2}"
)
"""Shapes that make a value a point in time rather than a quantity of one.

`2021년` is a year, not two thousand and twenty-one years, and a question asking
`몇 개월인가?` must not be told that value states years. The guard is on the
whole value rather than on the matched span, so a value containing a calendar
date reports no units at all, including a genuine mismatch stated elsewhere in
the same value: `2021년 착수, 총 3주` asked in months, and `2021년 기준 30분`
asked in hours, are both silent though each states a real same-family mismatch.

Four alternatives, each of which some value needs; the tests delete them one at
a time and every deletion changes an outcome.

A year followed by a month is a date whatever the year's width, which is the
branch that reads `21년 3월` and `25년 12월` as dates rather than as twenty-one
and twenty-five years. Two-digit years are ordinary Korean document notation, so
without it the issue's own headline question -- `기간` asked in `몇 개월` --
answers a date range with "the verified value states 년". The year is bounded
below at two digits: `2년 3월` is left alone because one digit is as likely to be
a duration as a date and nothing here separates them. What distinguishes this
branch from a real duration is the counter, not the number: `12년 6개월` is
twelve years and six months and stays a duration, because `개월` is not `월`.

A bare two-digit year is deliberately NOT caught. `21년` on its own really can be
twenty-one years, so it is left reading YEAR and disclosed in
`korean_measure_unit_mismatch` instead. Widening the four-digit branch to
`[0-9]{2,4}` would silence it, and that is the trade this declines.

The four-digit year branch is bounded on both sides, and the two bounds do
different work. The left-hand `(?<![0-9])` is what stops a genuine `10000년`
matching on its inner `0000년`. The right-hand `(?![0-9])` stops a four-digit run
that continues into more digits from being read as a year, which only a
contrived value reaches (`2021년12개월`).

The ISO branch earns its place narrowly. An ISO date on its own states no unit,
so the branch does nothing for `2021-03-15`; what it catches is a date with a
unit spelling run onto the end of it, `2021.03.15 일` and `2021-03-15일`, which
without it are read as stating days. It is the worst-paying of the four, because
the same whole-value suppression costs real caveats elsewhere: `2024/01/02 3주`
and `2021-03-15 (3일)` state genuine durations and go silent. Kept because a
false sentence is worse than a missing one, which is this rule's standing
preference; a span-local guard would take both, and that is a change of its own.

Its year is `[0-9]{2,4}` for the same reason the year+month branch's is, and
holding it at four digits while arguing two-digit years are ordinary notation
one branch above was the contradiction that got it widened: `21.03.15일` and
`25-01-15일` are dates by exactly the premise this file already accepts. Bounded
below at two digits and on the left, so `2.03.15일` and `12021.03.15일` are not
dates. What the branch still misses is a date with no year at all -- `03/15일`
needs two separators to be reached and has one -- which is disclosed in
`korean_measure_unit_mismatch` rather than chased with a fifth alternative.

Widening the year also widened what else looks like a date, and that is a third
cost on top of the two above: a dotted or dashed numeric triple whose first
component is two or three digits now reads as one. `12.5.3 버전, 3주 소요`,
`10.1.2 릴리스, 3주` and `10.0.0.1 서버, 3주` each state a genuine duration and
are silent, where before the widening they were caveated. A one-digit first
component still falls outside, so `1.2.3 버전, 3주` is unaffected -- which is
luck of the bound rather than a rule about version numbers.

`1500년` and `2000년간` are read as calendar years; both spellings are really
used for durations too, so that reading is honestly ambiguous and this rule
picks the date one.
"""


_VALUE_MEASUREMENT_RELAXED = re.compile(
    r"[0-9][0-9,.]*\s*[만억천조]?\s*(?P<unit>"
    + "|".join(
        re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
    )
    + r")"
    # No `_UNIT_SUFFIX` here, unlike `_VALUE_MEASUREMENT`. NOT because a match's
    # end is unread -- `finditer` resumes from it, so in general an optional
    # trailing group does change what is found later: `(?P<u>[ab])` reads "ab"
    # as a, b and `(?P<u>[ab])(?:b)?` reads it as a alone. It is inert here
    # because of what the two character sets are: every `_UNIT_SUFFIX_MEMBERS`
    # entry is Hangul, and every match must begin at a digit, so a start this
    # scan cares about can never fall inside a suffix that a longer match
    # swallowed. Ends do move; readings cannot. All three parts -- the premise
    # over the live tuple, the moving ends, the unchanged readings -- are
    # re-derived by `test_a_unit_suffix_would_be_inert_here`, not quoted here.
)
"""`_VALUE_MEASUREMENT` without the trailing lookahead, for the suppression test.

The lookahead is right for deciding what a value STATES -- `2년차` is a second
year of service, not two years -- but wrong for deciding whether the value
already carries the unit that was ASKED for. There it hid the asked unit and let
the caveat fire anyway: `3시간30분` answering `몇 시간인가?` was told the value
states minutes and that no conversion is applied, when the leading quantity is
exactly the hours asked for. A single space changed the outcome, because
`3시간 30분` passes the lookahead and `3시간30분` does not.

Suppression can only ever produce silence, so reading generously here is
safety-increasing in a way that reading generously in `_value_measure_units`
would not be. What it spends is caveats, and it does spend them -- see the last
paragraph, which is part of the design rather than a caveat about it.

Dropping the lookahead alone would have been wrong for one specific reason: it
is what makes `3달러` read as USD rather than as `달` plus a stray syllable, so
without it a question asked in months would find `달` inside `달러` and suppress
a caveat the value never earned. `달`/`달러` is the only one of the table's ten
prefix pairs that crosses canonical units; the other nine are two spellings of
one unit, where which of them matches cannot change any answer. So the whole of
what the lookahead was doing for THIS scan is discharged by reading `달러`
before `달`.

The alternation is sorted longest spelling first because that states the
intent, but length is not the property to preserve -- putting `달러` before `달`
is. Reversed-table and reverse-alphabetical orderings also satisfy it and read
identically; table order, shortest-first and alphabetical do not and read
differently. No count is quoted here on purpose: the numbers in this file have
rotted once already, so
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering`
partitions the orderings off the live table and asserts both halves instead.
This is the opposite of `_VALUE_MEASUREMENT`, where the lookahead does the work
and the order is free.

What no ordering buys, and nothing here does: a unit spelling that is merely a
syllable of an unrelated Korean word is still read as that unit whenever a digit
precedes it. `3분기` reads MINUTE, `1주년` reads WEEK, `80년대` reads YEAR,
`3 secondary` reads SECOND. Each of those suppresses a caveat the value had
earned, silently -- `몇 분인가?` answered `3분기 실적, 2시간 소요` named `시간`
before this scan existed and says nothing now, and the same goes for
`몇 주인가?` on `1주년 기념, 3개월 준비` and `몇 년인가?` on `80년대 후반, 3개월`.
These are lost caveats, not near-misses, and this is the noisier half of the
rule. The trade is deliberate: a lost caveat beats the wrong sentence the strict
reading produced on `3시간30분`. It is still a trade.
"""


def _value_states_asked_unit(value: str, asked_unit: str) -> bool:
    """Whether the value carries a quantity in the unit the question asked for.

    Read with `_VALUE_MEASUREMENT_RELAXED`, which is a strict superset of what
    `_value_measure_units` finds, so every unit that function reports is caught
    here too and this subsumes the plain equality test it replaced.
    """
    folded = nfc(value).casefold()
    return any(
        _MEASUREMENT_UNIT_SPELLINGS[match.group("unit")] == asked_unit
        for match in _VALUE_MEASUREMENT_RELAXED.finditer(folded)
    )


def _question_measure_unit(question: str) -> tuple[str, str] | None:
    """The unit a Korean measure question asked in -- its last, if it names two.

    Returned as (spelling, unit). The singular in a summary line is worth
    qualifying because a two-counter question falsifies it:
    `기간은 몇 년 몇 개월인가?` yields `개월`, so against a `2년` value this rule
    would name a mismatch against a question that did ask in years. That is
    latent rather than user-visible -- the cleaned label `기간은 몇 년` names no
    relation an ordinary KB holds, so the question is declined to the model and
    never reaches a verified answer to be caveated. Only a KB carrying a
    relation spelled `기간은 몇 년` could surface it.

    None when the question is not the flat attribute shape; when its label has no
    measure tail; when the tail carries a counter that names no unit
    (`몇 개인가?`) or no counter at all (`얼마나 되나요?`); or when what stands in
    front of the tail does not read as a relation.

    That last condition is what keeps a KB holding a relation literally named
    `몇 년` out of this rule. There the tail is the whole label, nothing is left
    in front of it, and `_korean_attribute_label_readings` reads the label whole
    -- the question is asking *for* that relation, not *in* that unit.
    """
    match = _KOREAN_ATTRIBUTE_QUESTION.match(question.strip())
    if match is None:
        return None
    label = " ".join(match.group("label").strip().split())
    tail = _KOREAN_ATTRIBUTE_LABEL_MEASURE_TAIL.search(label)
    if tail is None:
        return None
    spelling = tail.group("counter")
    # `.get`, so the two ways there is no unit to ask about -- a counter outside
    # the table and the `얼마나` branch, which captures no counter at all --
    # arrive at the same answer without a branch apiece.
    unit = _MEASUREMENT_UNIT_SPELLINGS.get(spelling)
    if unit is None:
        return None
    if not _label_readings_after_measure(label[: tail.start()].strip()):
        return None
    return (spelling, unit)


def _value_measure_units(value: str) -> tuple[tuple[str, str], ...]:
    """The units a value states, as this pattern reads them.

    Each is a (unit, spelling) pair, in the order stated.

    "As this pattern reads them" is load-bearing rather than hedging: `3시간30분`
    states hours and this returns only minutes, because the lookahead refuses a
    unit followed by a digit. `korean_measure_unit_mismatch` depends on that
    being the reporting reading and on a second, looser one deciding
    suppression.

    Empty for a value stating no quantity, and empty for a value carrying a
    calendar date -- `_CALENDAR_DATE` records what that costs.

    NFC because a value written in NFD spells `년` as two code points while the
    alternation is composed, and casefold because the Latin spellings in the
    table are lower-case. One consequence is worth naming: the spelling handed
    back is the folded one, so a value stating `3 Weeks` is reported as stating
    `weeks`.
    """
    folded = nfc(value).casefold()
    if _CALENDAR_DATE.search(folded):
        return ()
    return tuple(
        (_MEASUREMENT_UNIT_SPELLINGS[match.group("unit")], match.group("unit"))
        for match in _VALUE_MEASUREMENT.finditer(folded)
    )


def korean_measure_unit_mismatch(question: str, value: str) -> tuple[str, str] | None:
    """The (asked counter, stated unit) a unit caveat should name, or None.

    A mismatch only when the value states a unit in the same family as the one
    asked for and a second scan finds no quantity in the asked unit itself.

    Both halves are what a pattern reads, not what the value contains, and the
    sentence has to be put that way round: `_value_states_asked_unit` re-reads
    the value for the asked unit with the same quantity shape minus the
    lookahead, and anything that scan cannot see is not suppressed on. It reads
    `6개월` in `2년 6개월`, so a `몇 개월인가?` is suppressed. It does not read
    the won in `2천만원 (15,000달러)`, because the quantity shape admits one
    magnitude word and that number stacks two, so a `몇 원인가?` there is
    caveated with `달러` beside an answer whose leading figure is won.

    The two halves are read by different patterns, and that is deliberate rather
    than an oversight. What the value STATES comes from `_value_measure_units`,
    which refuses a unit run into the next character. Whether the value CARRIES
    the asked unit comes from `_value_states_asked_unit`, which does not -- with
    one pattern doing both, the same lookahead that correctly declines to read
    `2년차` as two years also hid the asked unit in `3시간30분` and `2년6개월`,
    and the caveat fired on a value whose leading quantity was exactly what the
    question asked for. Spacing decided it, which no reader would predict.

    The first same-family unit is reported, not the first unit. `30% 완료, 3주`
    asked in months states a ratio first and a duration second, and the duration
    is the part the question was about.

    The main causes of an accepted silence, rather than all of them: a value
    stating no number; a unit run into the next syllable (`2년차`); a value
    carrying a
    calendar date; a spelling outside the table; a suffix outside `_UNIT_SUFFIX`;
    a number that stacks magnitude words (`2천만원`), since the quantity shape
    admits at most one; and a number written in full-width digits (`３년`), which
    `[0-9]` does not admit and `nfc` does not fold away. `nfkc` would fold it, but
    `verinote.text.nfc` is the one normalizer the rest of the codebase compares
    through, and folding compatibility forms here alone would have this rule read
    a value differently from every other comparison made on it. A non-breaking
    space between the number and the unit is fine (`3<NBSP>년` states years), so
    this silence is specifically the digits. An earlier cross-family quantity
    does not silence a later same-family one.

    One silence is worth separating from those, because it is the only one where
    the value did earn a caveat and this rule loses it by misreading rather than
    by not reading. `_value_states_asked_unit` takes a unit spelling that is a
    syllable of an unrelated word as the asked unit, so `3분기 실적, 2시간 소요`
    asked in minutes suppresses as though it stated minutes and its real `시간`
    caveat is dropped; likewise `1주년 기념, 3개월 준비` asked in weeks and
    `80년대 후반, 3개월` asked in years. `_VALUE_MEASUREMENT_RELAXED` carries the
    class and the reason it is accepted.

    Wrong sentences this is known to produce. These are the ones that have been
    found, not a bound on what exists, and each round of review has added to
    them; read the list as open. They also have no single cause -- a two-digit
    year is not a lexical ambiguity and `second` is not Korean -- so do not
    generalise from it to decide whether some new input is safe.

    * A day of the month separated from its month by anything but whitespace.
      `_CALENDAR_DATE` reads a digit month and a day with nothing but whitespace
      between them, so `3월 15일`, `3월  15일` and `3월15일` are dates and
      `3월 중 15일`, `3월의 15일` and `3월 말 15일` are not, though those have a
      digit month too. `매월 15일` and `15일 마감` have no digit month at all.
      Every one of the misses is told it states `일` -- fifteen days rather than
      the fifteenth. A 정산일 or 마감일 relation holding `매월 N일` is ordinary
      contract data, which makes this the most reachable one found so far.
    * A date written with no year. The ISO branch needs two separators, so
      `03/15일` is not a date to it and is reported as stating `일`.
    * A value whose asked-unit quantity no pattern here can read, beside a
      same-family unit that one can. The suppression scan misses the first and
      the reporting scan finds the second, so the caveat names the second:
      `2천만원 (15,000달러)` asked in won reports `달러`, `5개년 계획 3주` and
      `３년 30주` asked in years report `주`, and `6월 및 30주` asked in months
      reports `주`. Each is a silence cause from the list above turned into a
      wrong sentence by a readable unit standing next to it.
    * A time of day. The guard has no time-of-day branch, and `시` is outside the
      spellings table while `분` is in it, so `3시 30분` and `오후 2시 15분` are
      reported to `몇 시간인가?` as stating `분` -- thirty minutes rather than
      half past three. Note that this one needs no strained question: `시간`
      asked in `몇 시간` is exactly the relation the counter names.
    * A bare two-digit year. `21년 3월` is read as a date, but `21년` alone is
      left reading YEAR, so `몇 개월인가?` answered `21년` is told it states
      years. Twenty-one years is a real duration and nothing here separates the
      two readings.
    * An English ordinal. `second` is in the spellings table as a unit, so
      `2 second review` is reported as stating `second`. The neighbouring
      `3 secondary reviews` is silent for an unrelated reason -- Latin continues
      past the spelling and the lookahead refuses it -- which does not reach the
      spaced form. The row stays, because `30 seconds` answering `몇 분인가?` is
      a real result and the question side can only ask SECOND through `몇 초`.
    * `몇 년인가?` answered `100주` (one hundred shares), and `몇 시간인가?`
      answered `5분` (five people, honorific): Korean spellings that mean two
      things, read here as the unit. These two also need a question asked in a
      unit the relation does not really measure.

    None of these is fixed here. #445 asks that a verified answer in another unit
    be caveated, not that every ambiguous spelling be resolved. The first two
    want a guard for "a point in time" wider than `_CALENDAR_DATE`, which is a
    change of its own rather than a widening of this one.
    """
    asked = _question_measure_unit(question)
    if asked is None:
        return None
    asked_spelling, asked_unit = asked
    found = _value_measure_units(value)
    if _value_states_asked_unit(value, asked_unit):
        return None
    asked_family = _MEASUREMENT_FAMILY[asked_unit]
    for unit, spelling in found:
        if _MEASUREMENT_FAMILY[unit] == asked_family:
            return (asked_spelling, spelling)
    return None


def _korean_attribute_label_readings(value: str) -> tuple[str, ...]:
    """Both ways to read a label whose last syllable might be a josa.

    A trailing `은`/`는`/`이`/`가` is genuinely ambiguous and no rule available
    here settles it. Stripping it -- what this did unconditionally -- turns the
    relation `단가` into `단` and `길이` into `길`. Keeping it turns a mis-typed
    `가격는?` into a relation named `가격는`, which no KB holds. Both are single
    guesses, each with its own failure class, and the evidence that decides
    between them is the schema: only it knows whether `단가` or `단` is a
    relation. So both readings are offered and the planner picks whichever the
    KB has, the same reasoning that kept the multi-hop guard out of the parser.

    Korean phonology narrows which reading is *likelier* -- `은`/`이` follow a
    syllable with a 받침, `는`/`가` follow one without, so the `가` in `단가`
    cannot be a josa -- but not which is *right*: `가격는` is exactly that
    impossible spelling and is still a mis-typed `가격`. Ordering by that rule
    is a reporting refinement, not part of this decision.

    The stripped reading comes first, preserving the reading this has always
    proposed; the un-stripped one is added, never substituted, so the candidate
    set is a superset of what this asked for before -- no relation the KB holds
    is dropped from the request. That is weaker than "nothing can stop
    resolving": a second reading that *also* names a real relation reaches the
    conflict gate, so a KB holding both `평` and `평가` on one subject turns
    `평가?` from an answer into an ambiguity report. That is the honest outcome
    for a question the KB genuinely does not disambiguate.

    Two tails are stripped before any of this and both are single guesses: the
    interrogative tail (`재인가?` loses `인가` and asks only for `재` -- see
    #443) and the measure tail. They differ in whether they may leave a label
    with nothing to ask for. The interrogative tail may, which is how
    `샘플사업의 인가?` loses its whole label and is declined. The measure tail
    may not, and what enforces that is not the strip but the readings it leads
    to: the measure reading is adopted only when it yields readings, and
    otherwise the label is read whole, exactly as it would be read without this
    rule. So this change declines no question that was not already declined.
    Unlike the josa, a measure tail is not a spelling of a relation name the way
    `단가` is, so there is no second reading for the schema to choose between;
    the one narrow case where it could be, `몇` read as "several", is named in
    `_KOREAN_MEASURE_QUESTION_TAIL`'s docstring.
    """
    label = " ".join(value.strip().split())
    # Adopting the measure reading is conditional on it surviving the rest of
    # the cleaning, because declining is not neutral: `샘플대상의 몇 개?` would
    # stop reaching `_reinterpret_empty_plan`, the gate that refuses a model
    # `no_answer` -- the reading Ask renders as `VERIFIED — engine (negative)`,
    # its strongest claim -- for a question the deterministic parser could plan
    # nothing for. Should the model then return an intent that itself plans
    # empty, that plan would also lose the direct-Datalog fallback a
    # deterministic intent keeps. Testing the finished readings rather than this
    # strip's own output is what covers the labels the two later strips would
    # empty, which `measured` alone does not see.
    measured = _KOREAN_ATTRIBUTE_LABEL_MEASURE_TAIL.sub("", label).strip()
    if measured != label:
        readings = _label_readings_after_measure(measured)
        if readings:
            return readings
    return _label_readings_after_measure(label)


def _label_readings_after_measure(label: str) -> tuple[str, ...]:
    """The rest of the cleaning: the interrogative tail, then the josa reading.

    Returns no readings at all when the label cleans away to nothing. That empty
    result is the signal the caller reads: a measure reading that cleans away is
    dropped in favour of reading the whole label.
    """
    label = _KOREAN_ATTRIBUTE_LABEL_TAIL.sub("", label).strip()
    stripped = _KOREAN_ATTRIBUTE_LABEL_JOSA.sub("", label).strip()
    if not stripped:
        # The whole label is a josa. Reading it as a relation name would claim
        # a shape this parser has always declined -- `{entity}의 {label}은?`
        # with a blank label lands here -- and then advise adding a policy
        # alias for a grammatical particle. A KB that really does hold a
        # relation named `은` is still reachable through the model, which sees
        # it in the schema hint.
        return ()
    if stripped == label:
        return (label,)
    return (stripped, label)


def _clean_korean_attribute_label(value: str) -> str:
    readings = _korean_attribute_label_readings(value)
    return readings[0] if readings else ""


def _looks_like_korean_attribute_question(raw_label: str, question: str) -> bool:
    tail = raw_label.strip()
    if question.rstrip().endswith(("?", "？")):
        return True
    # Deliberately NOT widened alongside the label cleaner. This decides whether
    # the flat attribute shape is claimed at all, and only for a question with no
    # `?` -- the branch above short-circuits otherwise -- so widening it changes
    # nothing for a `?`-terminated question. For one without a `?` it cuts both
    # ways: it would claim `샘플제품의 가격은 얼마` deterministically, but would
    # also flatten `샘플프로젝트의 담당자의 상사는 누구` into a single-hop label
    # that can only plan empty, taking it from the LLM, which can read it as a
    # two-hop lookup. Neither population is measured, so the cleaner is widened
    # and this is not. A `?`-terminated multi-hop question flattens either way;
    # that is a separate, larger defect this rule does not address.
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


def _korean_attribute_relation_candidates(raw_label: str) -> tuple[str, ...]:
    """Relation candidates for a Korean attribute label, both josa readings.

    Only the leading reading is expanded through `_attribute_relation_candidates`.
    The stripped reading leads, and today it is the only one that can be a
    synonym key: no key in that function's set ends in `은`/`는`/`이`/`가`,
    while the second reading ends in one by construction. So expanding it too
    is a branch nothing can currently reach.

    That is a fact about the key set, not a law. Add a key like `평가` -- which
    ends in a josa syllable and is in this issue's own list -- and `평가?` would
    need the second reading expanded to keep its synonyms, because the leading
    `평` is not a key. Expand both if that day comes.
    """
    readings = _korean_attribute_label_readings(raw_label)
    candidates = list(_attribute_relation_candidates(readings[0]))
    candidates.extend(readings[1:])
    return tuple(dict.fromkeys(candidates))


def _attribute_relation_candidates(label: str) -> tuple[str, ...]:
    normalized = label.strip()
    folded = normalized.casefold()
    if folded in {"목적", "목표", "프로젝트 목적", "사업 목적", "purpose", "objective", "goal"}:
        return PURPOSE_RELATION_CANDIDATES
    return (normalized,)


def _intent_field_names(schema: dict[str, Any]) -> tuple[str, ...]:
    """The property names the parser will accept as keys, in schema order.

    `_parse_query_intent_object` rejects any key outside this allow-list, so a
    hand-written copy of it turns a property added to the schema into an
    "unexpected fields" error -- the provider is told to emit the key and then
    refused for emitting it.

    Widening the allow-list is only half of accepting a property, though: the
    construction below has to read the key too, or it would be taken from the
    provider and dropped on the floor. `_intent_field_parsers` is what makes the
    second half follow the schema as well, and `_UNREAD_INTENT_FIELDS` is what
    stops the one step still done by hand from being silent.
    """
    return tuple(schema["properties"])


QUERY_INTENT_FIELDS = _intent_field_names(QUERY_INTENT_SCHEMA)


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
    # The remaining kwargs are the dispatch table applied to the payload, not a
    # second hand-written copy of the property names. Naming them here is what
    # made a schema addition land in the allow-list and then be discarded: the
    # key was admitted and never read. Now admitting it *is* reading it.
    return QueryIntent(
        kind=kind,
        **{
            field_name: parse(data, field_name)
            for field_name, parse in _INTENT_FIELD_PARSERS.items()
        },
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


def _parse_relation_candidates(data: dict[str, Any], field_name: str) -> tuple[str, ...]:
    raw = data.get(field_name)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError(f"{field_name} must be an array or null")
    if not all(isinstance(item, str) for item in raw):
        raise TypeError(f"{field_name} items must be strings")
    return tuple(raw)


def _parse_conjunctive_hops(data: dict[str, Any], field_name: str) -> tuple[ConjunctiveHop, ...]:
    raw = data.get(field_name)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError(f"{field_name} must be an array or null")
    hops = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"{field_name}[{index}] must be an object")
        if set(item) != {"subject", "relation", "object"}:
            raise ValueError(f"{field_name}[{index}] must contain subject, relation, and object")
        hops.append(
            ConjunctiveHop(
                subject=_parse_conjunctive_endpoint(item["subject"], f"{field_name}[{index}].subject"),
                relation=_parse_required_intent_target(item["relation"], f"{field_name}[{index}].relation"),
                object=_parse_conjunctive_endpoint(item["object"], f"{field_name}[{index}].object"),
            )
        )
    return tuple(hops)


def _parse_required_intent_target(raw: Any, field_name: str) -> IntentTarget:
    if not isinstance(raw, dict) or set(raw) != {"kind", "value"}:
        raise ValueError(f"{field_name} must contain kind and value")
    return IntentTarget(raw["kind"], raw["value"])


def _parse_conjunctive_endpoint(raw: Any, field_name: str) -> ConjunctiveEndpoint:
    if not isinstance(raw, dict) or set(raw) != {"kind", "value"}:
        raise ValueError(f"{field_name} must contain kind and value")
    return ConjunctiveEndpoint(raw["kind"], raw["value"])


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
    to emit, so on a field the schema leaves open (`value` and `reason` today) it
    means "absent" rather than "invalid". Where the schema pins an enum it means
    neither: "" is not one of the admitted values, so it is returned as-is for
    `_validate_schema_domains` to reject, the same as `operator="contains"`. A
    wrong *value* was always a violation; the enum fields simply have no spelling
    of null other than null. Which side a field falls on comes from
    `_blank_nullable_fields`, so the split follows the schema rather than a list
    kept here.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text and field_name in QUERY_INTENT_BLANK_NULLABLE_FIELDS:
        return None
    return text


def _declared_types(spec: dict[str, Any]) -> frozenset[str]:
    declared = spec.get("type")
    return frozenset(declared) if isinstance(declared, list) else frozenset({declared})


def _is_required_string_schema(spec: Any) -> bool:
    """Whether a sub-schema admits exactly the non-blank strings the parser takes.

    `minLength: 1` is not decoration here. `IntentTarget.value` and every
    relation candidate go through `_clean_required_string`/`__post_init__`, which
    refuse a blank; without the bound, `""` would be schema-legal output that the
    parse path reports as a provider violation.
    """
    return (
        isinstance(spec, dict)
        and spec.get("type") == "string"
        and isinstance(spec.get("minLength"), int)
        and spec["minLength"] >= 1
    )


def _is_nullable_string_property(spec: dict[str, Any]) -> bool:
    """Whether `_parse_optional_string` reads the property.

    Nothing beyond the declared type is required: that parser accepts every
    string and takes its domain from the property's own `enum`
    (`_validate_schema_domains`), so no string sub-shape can make schema-legal
    output be refused. This is what keeps a new nullable string enum property
    parsed, trimmed, and validated with no wiring, as #298 asks.
    """
    return _declared_types(spec) == frozenset({"string", "null"})


def _is_intent_target_property(spec: dict[str, Any]) -> bool:
    """Whether `_parse_intent_target` reads the property.

    `["object", "null"]` alone does not say so. That parser reads exactly one
    object shape -- the `{kind, value}` pair of QUERY_INTENT_TARGET_SCHEMA -- and
    refuses everything else per parse: an unexpected key, a missing one, a
    non-string half, and (through `IntentTarget.__post_init__`) a kind outside
    `INTENT_TARGET_KINDS` or a blank value. So each of those has to be off-schema
    for the property, or the parser turns schema-legal provider output into an
    `LLMError` naming the provider for a local mismatch.
    """
    if _declared_types(spec) != frozenset({"object", "null"}):
        return False
    if spec.get("additionalProperties") is not False:
        return False
    properties = spec.get("properties")
    if not isinstance(properties, dict) or set(properties) != {"kind", "value"}:
        return False
    if set(spec.get("required") or ()) != {"kind", "value"}:
        return False
    kind = properties["kind"]
    kind_enum = kind.get("enum") if isinstance(kind, dict) else None
    # The enum, not the declared type, is what pins the legal values: an enum of
    # target kinds admits only those four strings whatever `type` says, and an
    # absent or empty one admits either every string or none at all. Both of
    # those are schemas `IntentTarget` cannot promise to accept.
    if not kind_enum or not all(value in INTENT_TARGET_KINDS for value in kind_enum):
        return False
    return _is_required_string_schema(properties["value"])


def _is_relation_candidates_property(spec: dict[str, Any]) -> bool:
    """Whether `_parse_relation_candidates` reads the property.

    `["array", "null"]` alone does not say so either: that parser reads an array
    of non-blank strings and refuses any other element per parse. An array of
    objects, or a `prefixItems` tuple whose leading entries are not strings, is a
    schema-legal array it would report as a provider violation.

    The non-blank bound is asked of every array property, not just
    `relation_candidates`, because this is the relation-candidate parser: what it
    reads is that array, and `__post_init__` refuses a blank candidate. A
    property meaning to admit blank items is a shape no parser here reads, so it
    stops at import rather than being handed to this one on the strength of
    sharing a type.
    """
    if _declared_types(spec) != frozenset({"array", "null"}):
        return False
    if "prefixItems" in spec:
        return False
    return _is_required_string_schema(spec.get("items"))


def _is_conjunctive_hops_property(spec: dict[str, Any]) -> bool:
    if _declared_types(spec) != frozenset({"array", "null"}):
        return False
    if spec.get("minItems") != 2 or spec.get("maxItems") != 2:
        return False
    item = spec.get("items")
    if not isinstance(item, dict) or item.get("additionalProperties") is not False:
        return False
    return set(item.get("properties") or ()) == {"subject", "relation", "object"}


def _is_conjunctive_three_hops_property(spec: dict[str, Any]) -> bool:
    if _declared_types(spec) != frozenset({"array", "null"}):
        return False
    if spec.get("minItems") != 3 or spec.get("maxItems") != 3:
        return False
    item = spec.get("items")
    if not isinstance(item, dict) or item.get("additionalProperties") is not False:
        return False
    return set(item.get("properties") or ()) == {"subject", "relation", "object"}


# `kind` is parsed by hand in `_parse_query_intent_object` and so is not in the
# table: it is the only non-nullable property, a missing one is a KeyError rather
# than a None, and it is converted to QueryIntentKind before the other fields are
# read. Every other property is matched against the shapes below, in order.
_INTENT_FIELD_PARSERS_BY_SHAPE: tuple[
    tuple[Callable[[dict[str, Any]], bool], Callable[[dict[str, Any], str], Any]], ...
] = (
    (_is_nullable_string_property, _parse_optional_string),
    (_is_intent_target_property, _parse_intent_target),
    (_is_relation_candidates_property, _parse_relation_candidates),
    (_is_conjunctive_hops_property, _parse_conjunctive_hops),
    (_is_conjunctive_three_hops_property, _parse_conjunctive_hops),
)


def _intent_field_parsers(
    schema: dict[str, Any],
) -> dict[str, Callable[[dict[str, Any], str], Any]]:
    """A parser per schema property, chosen by the property's whole shape.

    This is the half of #298 that deriving the allow-list left open. With the
    kwargs written out by hand, a property added to the schema was admitted by
    `_intent_field_names` and then never read, so its value was taken from the
    provider and dropped -- an off-enum or blank value came back as None instead
    of being rejected. Dispatching off the schema means the construction follows
    it too: a new `["string", "null"]` property is trimmed, blank-normalised off
    the enum fields, and held to its enum with no wiring step, because it is
    parsed by the same function `operator` is.

    Matching on the declared type alone was too coarse to keep that promise
    honest. Each parser reads one shape, not one type: `_parse_intent_target`
    reads the `{kind, value}` target and `_parse_relation_candidates` reads an
    array of non-blank strings, and both refuse anything else per parse, where
    `parse_query_intent` relabels the refusal as "the provider violated the
    schema". So a differently shaped `["object", "null"]` or `["array", "null"]`
    property -- a nested object with its own properties, an array of objects --
    would pass this guard on its type and then blame the provider for output its
    own schema allows. Assigning a parser only when the property is the shape
    that parser reads moves that mismatch back to import, where it is: a
    half-finished schema change, not a provider error.

    An unmatched shape fails here rather than defaulting to a parser: guessing
    would hand a number or a nested object to the string parser.
    """
    parsers: dict[str, Callable[[dict[str, Any], str], Any]] = {}
    for field_name, spec in schema["properties"].items():
        if field_name == "kind":
            continue
        parser = next(
            (parse for matches, parse in _INTENT_FIELD_PARSERS_BY_SHAPE if matches(spec)),
            None,
        )
        if parser is None:
            raise RuntimeError(
                f"query intent schema property {field_name!r} is {spec!r}, which "
                "matches no shape any parser in _INTENT_FIELD_PARSERS_BY_SHAPE "
                "reads (a nullable string, the IntentTarget kind/value object, or "
                "a nullable array of non-blank strings); add a parser and the "
                "check for its shape rather than letting the property be parsed "
                "as something it is not"
            )
        parsers[field_name] = parser
    return parsers


_INTENT_FIELD_PARSERS = _intent_field_parsers(QUERY_INTENT_SCHEMA)


def _unconsumed_intent_fields(field_names: tuple[str, ...]) -> frozenset[str]:
    """Allow-listed property names `QueryIntent` has no field to hold.

    Both halves are derived -- the schema's properties against the dataclass's
    actual fields -- so there is no third list claiming what the parser reads to
    be trusted or to drift. Previously this compared the schema to a hand-written
    `_PARSED_INTENT_FIELDS` mirroring the kwargs, which meant adding a name there
    without wiring the kwarg re-opened the silent drop with the guard quiet.
    """
    return frozenset(field_names) - {dataclass_field.name for dataclass_field in fields(QueryIntent)}


# Checked once, at import, rather than per parse. Both halves of the comparison
# are fixed at import, so a mismatch is a half-finished schema change and cannot
# be provoked by anything a provider sends -- which is also why it must not be
# raised from inside the parse path, where `parse_query_intent` converts
# KeyError/TypeError/ValueError into LLMError and would report a local wiring bug
# as "the provider violated the schema", sending the reader after the wrong
# thing. Failing here rather than skipping the name is the call `__post_init__`
# makes for the dataclass: the construction now passes every admitted property as
# a kwarg, so a property with no field would be a TypeError raised per parse and
# laundered into "the provider violated the schema".
_UNREAD_INTENT_FIELDS = _unconsumed_intent_fields(QUERY_INTENT_FIELDS)
if _UNREAD_INTENT_FIELDS:
    raise RuntimeError(
        "query intent schema has properties QueryIntent has no field for: "
        f"{', '.join(sorted(_UNREAD_INTENT_FIELDS))}; add them as fields on the "
        "QueryIntent dataclass"
    )


def _clean_required_string(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text
