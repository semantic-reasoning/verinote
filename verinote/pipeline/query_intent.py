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
  present. Nothing in THIS table reads `월` as a month of the year either: the
  row is absent, so a `6월` that really did mean six months states no unit this
  rule can see, and is silent for want of any reading rather than by a
  judgement between two. `_TIME_POINT` does read digits run into `월`, but as a
  month term inside a longer shape rather than as a unit, so the two do not
  disagree.
  The exclusion is live on the suppression scan too, which is what makes
  `6월 및 30주` asked in months name the weeks. Admitting `월` there was measured
  against #451 and declined: it silences `3월 15일간`, `3월 내 15일 소요`,
  `3월 계약 15일 소요` and their siblings, values whose duration other tests
  assert is caveated, and it reaches the `N월 ..., N주` shape at large, where
  the `3월` is the month of the year far more often than a count of them.
  The reach is the DIGIT month only -- `전월 대비 3일 단축` and `매월 15일간`
  keep their caveats, since the scan needs a digit in front of the `월` --
  which narrows the cost without
  changing the verdict. Declining a `월` that falls inside a `_TIME_POINT` span
  does not rescue the narrower cases either: `2년 3월` and `10000년 3월` match no
  branch of that pattern.
* `일` is present, unlike `월`: `3일` is three days far more often than it is
  the third of the month. That is a judgement about which reading is commoner,
  not a guarantee that the other one is caught. What catches the other reading
  is `_TIME_POINT`, which needs a month term in front of the day, in the sense
  that pattern defines: `3월 15일`, `3월 중 15일` and `매월 15일` are dates.
  `15일 마감` has no month term at all and does state `일`. See
  `korean_measure_unit_mismatch`.
* `개년`. It fired on `5개년 계획`, which is the name of a plan rather than a
  duration.
  Live on the suppression scan too, so `5개년 계획 3주` asked in years names the
  weeks -- a wrong sentence #451 records. Fixing it means reading `5개년` as
  possibly stating years after all, which is the judgement this row was excluded
  for, so the two cannot both stand.
* `$`, `₩`, `€`. Every quantity here begins at a digit, and these precede their
  number, so no row in this table could reach them. `%` is read because it
  follows the number. That asymmetry is by construction; it is not an omission
  a row would restore.
  What #451 adds is not a reason to restore one but a name for what the
  omission costs. A symbol-led sum states no unit on either scan, so where a
  readable same-family unit stands beside it the caveat names that one:
  `₩20,000,000 (15,000달러)` asked in won reports `달러`, before this change and
  after. That is the general form `korean_measure_unit_mismatch`'s last bullet
  states -- an accepted silence with a readable neighbour is a latent wrong
  sentence -- reached by a cause that is neither the number nor a missing row,
  but the side the symbol sits on.
"""

_UNIT_SUFFIX_MEMBERS = ("간", "가량", "정도", "쯤", "짜리")
"""The particles that may follow a quantity and leave it readable as one.

Read by three things now, so the property it claims matters: `_UNIT_SUFFIX`
builds the value patterns from all of it, and `_TIME_POINT`'s day branch reads a
subset (`_DAY_DURATION_SUFFIXES`) for the converse property, which two members
do not have. What this tuple asserts is only the first direction -- a member
leaves a quantity readable -- and borrowing it for the second is the mistake
that partition exists to record.

Every member is Hangul, and `_VALUE_MEASUREMENT_RELAXED` is dropped from that
pattern on exactly that ground. A test that restated the members as its own
literal would go on passing if a non-Hangul one were added here, which is the
case the argument does not survive: a digit member makes `3년2주` read as `년`
alone and a `몇 주인가?` against it answer "the verified value states 주".
"""

_DAY_DURATION_SUFFIXES = ("간", "가량", "짜리")
"""The suffixes whose presence proves an `N일` is a duration.

A subset of `_UNIT_SUFFIX_MEMBERS`; `_TIME_POINT`'s day branch consults this and
not that. The criterion is not "does it mean approximately" -- `가량` does, and
is here -- but whether the suffix attaches to a point in time as readily as to a
quantity. The test is whether `3시X` is ordinary Korean: `3시쯤`, `3시 정도` and
`3시께` are, `3시가량` is not.

That is a lexical judgement about Korean, stated so it can be argued with rather
than proved from anything here. What the file does supply is `3월 15일경`, where
the standard date approximator has never been listed and the value reads as a
date. Reading the same split off the meanings arrives at it independently: `간`
is "for the duration of", `짜리` is "worth of", and `가량` approximates a
quantity rather than a point.

Both halves are literal, and neither is derived from the other. An open class
needs two things, and deriving one half gives only one of them: a safe default,
so an unclassified member costs a caveat rather than inventing one, and a
tripwire that refuses to take that default silently. The default here is safety
-- a suffix in neither tuple is not consulted, so the day stays a date -- and
the tripwire is the union equality the tests assert, which fails and names the
decision someone has to make. Derive either half and that equality holds by
construction: `께` would join `_UNIT_SUFFIX_MEMBERS`, land on a side by
arithmetic, and say nothing. Under exclusion it landed on this one, which made
`3월 15일께` a wrong sentence.
"""

_DATE_APPROXIMATOR_SUFFIXES = ("쯤", "정도")
"""The `_UNIT_SUFFIX_MEMBERS` that approximate a point in time as readily as a
quantity, so their presence proves nothing about the day they follow.

Literal rather than the complement of `_DAY_DURATION_SUFFIXES`, for the reason
given there.
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
"""One quantity stated inside a value: ASCII digits, at most one of four Korean
magnitude words, and a unit spelling.

`[만억천조]?` is one character, not a run, so `3만원` is read and `2천만원` is
not; it is those four and no others, so `2백만원` is not either; and the digits
are `[0-9]` rather than `\\d`, so `３년` is not. All three of those bounds are
narrower than `_VALUE_MEASUREMENT_RELAXED`'s, which is a statement about the
NUMBER and not about the two patterns as wholes: since #453 the relaxed one
carries a refusal of its own in `_UNIT_SHADOW_GUARD`, one this pattern already
makes through its trailing lookahead. The asymmetry has a direction. This
pattern decides what a value STATES and its output is put in front of a reader,
so widening it ADDS sentences and needs a sweep of its own; the relaxed one
decides only whether to stay silent. #451 widened the relaxed number and left
this one where it was, which is why those three are silent here and suppress
there.

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

_MONTH_WORD_MEMBERS = (
    "매월", "매달", "금월", "익월", "내월", "당월", "전월", "차월",
    "다음 달", "이번 달", "지난 달", "내달",
)
"""The month terms this file recognises as words rather than digits.

Like `_UNIT_SUFFIX_MEMBERS`, this exists as a tuple so a test can assert over
the live set instead of over a copy of it. `_TIME_POINT` states what a month
term does; this only lists the word forms of one.

The list is the ones that have been found, not the ones that exist -- Korean
has more ways to name a month than any closed tuple holds, and a word outside
it puts its value back in `korean_measure_unit_mismatch`'s residue rather than
into any error. `금월`, `내월` and `내달` were added after the first draft
shipped without them.

A word list fails in two directions and only one of them is above. The other is
a member matching inside a longer word, which is why the alternatives carry a
left bound -- see `_MONTH_OF_YEAR`. That bound is on digits only, so the digit
spelling `1차월` is excluded and the Sino-Korean numeral spelling `일차월` is
not; a member that is a common word tail would need more than a bound.
"""

_MONTH_PART_MEMBERS = ("의", "중", "초", "말")
"""The parts that may stand between a month term and its day, named for the tests.

Closed on purpose -- see `_TIME_POINT`, which is where the rule lives.
"""

_MONTH_OF_YEAR = (
    r"(?:[0-9]{1,2}\s*월|"
    # The word alternatives carry a left bound the digit one must not have.
    # Without it `차월` matches inside `1차월`, which is month one of a
    # programme and not a point in time at all, and `1차월 3일 소요` lost a
    # caveat it had earned. Digits are the only thing excluded, so `해당월` and
    # `익익월` keep matching on their tails -- an accident, but one that lands
    # on the right answer, and a Hangul bound would give it up.
    + r"(?<![0-9])(?:"
    # Every member is Hangul plus at most one space, so they join raw -- the
    # premise `_UNIT_SUFFIX` joins on -- and the space is relaxed so
    # `다음달 1일` reads like `다음 달 1일`.
    + "|".join(w.replace(" ", r"\s*") for w in _MONTH_WORD_MEMBERS)
    + r"))"
)

_TIME_POINT = re.compile(
    # A year in front of the month term belongs to the same date, so the span
    # reaches back over it. No left bound here, unlike the year+month branch --
    # see the docstring, where the two bounds are told apart.
    r"(?:[0-9]{2,4}\s*년\s*)?"
    + _MONTH_OF_YEAR
    + r"\s*(?:" + "|".join(_MONTH_PART_MEMBERS) + r")?\s*[0-9]{1,2}\s*일"
    # Not a day of the month if what follows proves it is a duration. That is
    # `_DAY_DURATION_SUFFIXES`, a subset of `_UNIT_SUFFIX_MEMBERS` and not that
    # tuple -- see the docstring for why the two differ.
    + r"(?!(?:" + "|".join(_DAY_DURATION_SUFFIXES) + r"))"
    # The minute and the second belong to the same clock time, and each is
    # optional independently of the other -- nested, `3시 20초` left its `초`
    # outside. Neither carries a lookahead of its own; the docstring weighs the
    # two candidates for one.
    r"|[0-9]{1,2}\s*시(?![가-힣])(?:\s*[0-9]{1,2}\s*분)?(?:\s*[0-9]{1,2}\s*초)?"
    r"|['’‘]\s*[0-9]{2}\s*년"
    r"|(?<![0-9])[0-9]{2,4}\s*년\s*[0-9]{1,2}\s*월"
    r"|(?<![0-9])[0-9]{4}\s*년(?![0-9])"
    r"|(?<![0-9])[0-9]{2,4}\s*[-./]\s*[0-9]{1,2}\s*[-./]\s*[0-9]{1,2}"
)
"""Shapes that make a value a point in time rather than a quantity of one.

`2021년` is a year, not two thousand and twenty-one years, and a question
asking `몇 개월인가?` must not be told that value states years.

The guard is span-local: `_value_measure_units` drops the quantities that
overlap a match of this pattern and reports the rest, so a duration standing
outside every match is still reported. `2021년 착수, 총 3주` and
`2021년 기준 30분` each state a real same-family mismatch and each now names it;
so do `3시 시작, 3주 소요`, `2024/01/02 3주`, `2021-03-15 (3일)` and
`12.5.3 버전, 3주 소요`. #452 is where the whole-value rule that lost all of them
is recorded, and #450 is where the class it lost had last been widened.

Overlap has two near neighbours and is neither of them, and each difference has
a witness. Masking the span out of the string would let `_VALUE_MEASUREMENT`'s
trailing lookahead see the fill character instead of the real neighbour, so
`3주2021-03-15` would be read as stating weeks -- the lookahead refuses `3주`
there because a digit follows it, and that refusal is the whole precision the
fill would spend. Dropping the overlapping match leaves every character where it
was.

Containment -- dropping only the quantities a span fully covers -- is the nearer
neighbour, and the one a later simplification reaches, since it reads as a tidier
way of writing the same rule. It is not the same rule, because a quantity can
cross a span's boundary in either direction and containment keeps every one that
does. In `2021-03-15일` the span is (0, 10) and `_VALUE_MEASUREMENT` reads `15일`
at (8, 11), crossing the right edge; containment keeps it and reports the value
as stating days, the one reading the ISO branch exists to refuse. In
`10000년 3월 15일` it is the left edge: the year prefix takes no left bound, so
the span opens at the inner `0000년` at (1, 13) while the real `10000년` runs
(0, 6), and containment calls ten thousand years a duration. The arguments below
that rest on the difference are not only the ISO one, and they are not a fixed
list either -- any passage whose value is silent because a quantity STRADDLES a
span is one, since containment would report that quantity. A quantity sitting
wholly inside a span is dropped by both rules, so `매월 15일`, `3시 30분` and
`2021년` rest on nothing here. Those found so far:
`3월 15일쯤` and `매월 15일정도` for the day approximators, `3시 30분간` and its
siblings for the swallowed clock tail, the two long years for the prefix's
missing left bound, and `12.5.33주` and `2021-03-153일` for the flush-quantity
residue two paragraphs down. The clock tail and the long years are visible only
under this widened pattern, so a probe run against the pre-#452 one
under-reports the class.
`test_a_quantity_is_dropped_by_overlap_and_not_by_masking_or_containment`
recomputes both neighbours and asserts each differs.

What a span COVERS therefore matters in a way it did not before. Under the
whole-value rule a branch only had to prove the value held a point in time; now
a component left outside the span is read as a quantity beside it, so each
branch has to run from the coarsest component of its expression to the finest.
Two branches below take a component for that reason alone and for no other: the
day branch takes an optional year, and the clock branch takes an optional minute
and second.

The set of components that can cost anything is closed, and closed by something
other than a word list -- it is the calendar and the clock, year, month, day,
hour, minute, second, which is not a class the next input extends. Narrower
still: a component can only cost a caveat if it can be read as a unit, and
`_MEASUREMENT_UNIT_SPELLINGS` decides that. `월` and `시` are absent from that
table on purpose, each for a reason given beside it, so no value can be said to
state them and no span has to reach them. `_VALUE_MEASUREMENT_RELAXED` draws
from the same table and feeds only `_value_states_asked_unit`, whose one output
is suppression, so what it decides is whether a caveat is suppressed and never
which unit a caveat names, and it does not widen the set. Since #453 it can
START a caveat, by refusing a spelling it used to read, but the one it starts
still names a unit `_value_measure_units` reported, so the set is untouched
either way and this argument is unaffected.

The premise is checkable on both of its sides, and
`test_a_span_covers_every_component_a_value_could_be_said_to_state` checks them
separately, because only one of them is live in the same sense. It recomputes
the intersection from `_MEASUREMENT_UNIT_SPELLINGS`, so adding `월` or `시` to
that table fails it for want of a fixture. There is no list of components to
recompute the other side from, so that side is pinned from the pattern instead:
the unit spellings occurring literally in `_TIME_POINT.pattern` are `년`, `달`,
`분`, `일` and `초`, and a branch naming a sixth -- `주`, say, for a week of the
month -- fails the test. `달` is on that list only through the members of
`_MONTH_WORD_MEMBERS` that spell it, each of which puts a Hangul syllable in
front of it with at most a space between, so a digit run can never reach a `달`
inside a span and no value can be said to state months there. The test derives
that from the live tuple rather than taking this sentence for it.

Say the rule as "the quantities overlapping a span are dropped" rather than as
"a guarded value is not read", because the value IS still read:
`_value_states_asked_unit` does not consult this guard, so
`_value_states_asked_unit("매월 15일", "DAY")` is True while
`_value_measure_units("매월 15일")` is empty. What ends a caveat is the reported
list, whatever the suppression scan sees. That is the same two-scan asymmetry
`korean_measure_unit_mismatch` describes.

What span-local costs is the accidental cover the whole-value rule was giving
to every quantity standing outside a span. That is a total statement rather
than a list: the guard used to empty the reported list whenever it matched
anywhere, so every wrong sentence `korean_measure_unit_mismatch` records was
silenced whenever some other part of the value happened to be a point in
time, and span-local withdraws that from all of them at once.
`2021년 계약, 15일 마감` is now told it states `일`, `2021년 착수, 21년` that
it states `년`, `2021년 계약, 2 second review` that it states `second`,
`2021년 기준, 6월 및 30주` that it states `주`, and
`2021년 계약, 15,000달러` that it states `달러` -- one witness per
entry would just be the list of entries again. None of them is a new
misreading: each value reads the same way standing alone and has since before
#450. What changed is the reach, and it changed for the whole list rather
than for the members someone thought to name.

What is total is the WITHDRAWAL, not the outcome, and the two must not be read
as one. Whether a caveat then names the entry is decided after this guard has
run, by `korean_measure_unit_mismatch`, and the account below is read off that
function's three ways of returning nothing rather than collected from examples.
It returns nothing when the question names no unit at all; when the suppression
scan finds the asked unit in the head's own notation; and when its loop finds
nothing to name. That last one is the one with several roads into it: the
entry's quantity may be covered by a span, or never read at all --
`_VALUE_MEASUREMENT`'s trailing lookahead is `(?![가-힣0-9A-Za-z])`, so a unit
run into Hangul, a digit or Latin is refused, and the `15일` in
`3월 15일과 20일` is never read for that reason, as is the one in
`매월 15일동안 3주` -- or read, and outside every span, and passed over anyway
for measuring something else, which is what becomes of a `15일` when the
question asks in `몇 원인가?`. A fourth thing decides not whether but WHICH:
only the first same-family quantity is returned, so an earlier one outranks the
entry's. Those are the exits, so a mechanism not among them would have to live
somewhere other than this function.

The outcome therefore turns on the question as much as on the head, and the two
witnesses differ in exactly that way. `3시 5분` is silent whatever it is asked,
because the `5분` is the clock's own minute and sits inside the span -- this
guard working, not failing, under every question the rule can put.
`2021년, 100주` is silent asked in `몇 년인가?`, where `_value_states_asked_unit`
reads the head's `2021년` as years and returns the answer it gave before this
change; asked in `몇 개월인가?` the same value now states `주` where it was
silent before, so it is no counter-example to the withdrawal being total.
`2021년 계약, 3주 소요, 15일 마감` is the third: its `15일` IS reported and still
never reached, because the three weeks stand in front of it. So "every entry
loses the cover" is exact, and "every entry is caveated now" would be false.

Two of them are argued separately below because their prospects differ, not
because the cost stops at two. #460's residue -- a bare `N일`, a bare
two-digit year -- is where nothing is left in the value to read. The elided
second member of a date is where something is.

The reach is widest where the head of a second date is elided, and that is
ordinary Korean rather than a corner. `3월 15일~20일`,
`매월 15일, 30일` and `3월 15일 및 20일` each name two days of one month; the
branch takes the first and the second stands outside every span, so each is now
told it states `일`. `3시 30분 ~ 45분` is the same shape on the clock and
`2021년, 22년` on the year. This one is NOT #460's residue, because a month term
IS left in the value to read -- but nothing measured so far reads it whole. A
bounded continuation on the day reaches the tilde spelling and none of the
hyphen, comma, `·`, `및`, `과` or `/` ones, and the separator set is an open
lexical class of the kind the day branch's own suffix rule exists to refuse.
Suppressing a leaked quantity whose unit a span already states covers every
separator, but only where the head's own component survives
`_VALUE_MEASUREMENT`'s lookahead, so it would fix `3월 15일, 20일` and not
`3월 15일과 20일`, one particle apart. `korean_measure_unit_mismatch` carries the
class and the tests record it.

Every alternative is needed by some value, and so is every member of the two
tuples: delete any one of them and some value changes its answer, which is what
the tests beside this file are built to catch. No count is quoted here -- the
pattern itself is the list.

A day of the month is a month term, an optional month part, and the day. The
month term is one or two digits run into `월`, or one of `_MONTH_WORD_MEMBERS`.
`개월` cannot be reached, because the digits must run straight into `월` with
only whitespace between -- the same property that keeps `12년 6개월` a duration
one branch below. This replaced a branch that required the day to stand
immediately beside a digit month, and with the part group empty it reads
exactly what that branch read, so no value that was a date stops being one.
`3월 15일`, `3월 중 15일`, `3월의 15일` and `매월 15일` are now read alike.

A year in front of the month term is part of the same date, and the branch takes
it as an optional prefix so that the span reaches back over it. Without the
prefix, `2021년 3월 15일` is matched by the year+month branch, which stops at
`3월` and leaves a `15일` outside the span to be read as fifteen days -- the day
branch cannot take it instead, because it must begin at the month term and the
earlier match has already consumed past it. The prefix is `[0-9]{2,4}` for the
reason the year+month branch's year is, and the two are written out separately
rather than shared because their bounds differ, which is two paragraphs down.

No left bound is placed on the number, and that is deliberate rather than an
omission: the branch this replaced had none, so `123월 15일` matches on its
inner `23월 15일` and is silent, and adding a bound would make that value newly
caveated -- a caveat gained, which this rule may not do quietly. The word
alternatives are bounded, for the opposite reason given beside them. One
consequence of the digit month being unbounded is that the width it admits is
decoration: `[0-9]{1,2}` and `[0-9]` and `[0-9]{1,3}` all read the same values,
because a longer run simply matches further in. The same is true of the clock
hour. Only the DAY's width is load-bearing, since the day must start where the
month term ended.

The year prefix takes no left bound, and that is the bound the year+month branch
does take. The two are doing different work. There the year alone is what makes
the value a date, so a spurious inner match silences a genuine duration and
`10000년` must not read as `0000년`. Here the month and the day have already
decided the value is a date; the prefix only chooses how much of it the span
covers, and a longer span can only remove a caveat. Bounded, `10000년 3월 15일`
and `12021년 3월 15일` have a leading digit left over and are told they state
years. A one-digit year is still left outside, so `1년 3월 15일` and
`2년 3월 15일` have their `1년` and `2년` read as durations and ARE caveated --
the same judgement the year+month branch makes on `2년 3월`, and the only place
a component that could belong to the date's own notation is deliberately left
out of the span.

The day refuses a `_DAY_DURATION_SUFFIXES` tail, for the reason the clock hour
refuses a Hangul one. `_UNIT_SUFFIX` makes `15일간` fifteen days, `15일가량`
about fifteen days and `15일짜리` a fifteen-day one, so a day wearing any of
those cannot be the fifteenth of anything; without the lookahead the branch read
the `15일` inside and called `매월 15일간` a point in time, losing a caveat on a
value with no point in time in it at all, which is not a loss this guard's
bargain covers.

What the branch consults is bounded morphologically, and that is the boundary
worth stating because it is the one that closes. "Means a duration" is an open
lexical class -- `간`, `가량`, `짜리`, then `동안`, `남짓`, `내내`, `이상`,
and every free word that implies a span -- so enumerating it would narrow one
shape and leave its neighbours, the failure #450 was opened against. "Is bound
to the number" is closed, and that is all the argument needs: every candidate is a
member of `_UNIT_SUFFIX_MEMBERS`, which is enumerated, so nothing outside it can
be consulted. So the rule is consult only what is flush against `일`, and among
those not one that approximates a point in time as readily as a quantity. A
free word after a space is not consulted, and `korean_measure_unit_mismatch`
discloses what that costs.

`_DAY_DURATION_SUFFIXES` is bound and closed, but it is NOT the set of bound
suffixes, and reading it as one is the mistake this passage has made more than
once. Two filters stand between them, and each has a member to its name: `정도`
is dropped for not being bound at all, a 명사 whose standard spelling is spaced;
`쯤` is bound, written flush, and dropped anyway for approximating a point in
time. Either filter can take any member, so read the tuple as what survived both
rather than as a class -- the identity is what keeps going false as members move
between the two halves.

A space therefore decides the verdict for a bound suffix, and that is the
boundary rather than the defect `_VALUE_MEASUREMENT_RELAXED` exists to fix. There
`3시간30분` and `3시간 30분` are both standard and got different answers; here
`15일간` is standard and `15일 간` is a misspelling. Where the free form is the
standard one there is no flip: `정도` is a noun, and `매월 15일정도` and
`매월 15일 정도` are both silent.

`_DAY_DURATION_SUFFIXES` is a subset of `_UNIT_SUFFIX_MEMBERS`, and that is the
second half of the rule. `_UNIT_SUFFIX` is the set of particles that leave a
quantity readable as one -- the only property its own docstring claims. The day
branch needs the converse, that the particle proves its `N일` is not a date, and
`쯤` and `정도` do not have it: an approximated date is still a date. `3월 15일경`
is the same idea wearing a suffix this file has never listed, and it reads as a
date. Taking `쯤` would have given two spellings of one meaning two answers, and
it un-fixed this issue's own headline -- `매월 15일` a date and `매월 15일쯤` a
duration, one particle apart.

This is the one place #450 made a value newly caveated rather than newly silent,
and it did so knowingly: `3월 15일간` was silent before #450 too, so
the lookahead reaches back past #450's own additions. Which values gain
depends on the baseline, and both readings are true of different ones: against
this pattern with the lookahead removed, any month term can gain; against
`e7ac2a7`, only a digit month can, because the older guard had no word months to
silence and so nothing to stop silencing. The tests derive each from its own
baseline rather than restating either here. Every gained value states days, so
each added caveat is right, and the "may not do quietly" above is the standard
being met rather than evaded.

`_MONTH_PART_MEMBERS` is closed, and closing it is the whole precision of the
branch. Admitting any single Hangul syllable instead also takes
`3월 내 15일 소요`, `3월 후 15일 소요`, `매월 약 3일` and `전월 대비 3일 단축`,
each of which states a real duration. What the closed set costs is
`3월 중 15일 소요` and `3월 말 15일 소요`, honestly ambiguous and read here as
the fifteenth.

A clock hour is a point in time and `시간` is a duration, and the tables are
what separate them: `시` is in neither `_MEASUREMENT_UNIT_SPELLINGS` nor
`_KOREAN_MEASURE_COUNTER` while `시간` is in both, so digits running into `시`
state no unit this file can read and can only be a clock. The lookahead is
`(?![가-힣])` rather than `(?!간)` because `3시그마` and `5시리즈` are words,
not times; it is not `(?![가-힣0-9])` because `3시30분` is half past three. A
clock time with a Hangul tail -- `3시부터`, `3시경`, `3시반` -- falls outside
and needs nothing, since it states no unit for a caveat to be wrong about.

The minute and the second belong to the same clock time, so the branch takes
them and the span covers them. `3시 30분` is half past three, and with the span
ending at `3시` the `30분` outside it reads as thirty minutes, which asked in
`몇 시간` is a wrong sentence. The two tails are optional and INDEPENDENT, so
`3시`, `3시 30분`, `3시 20초` and `3시 30분 20초` all match. Nested, the second
could only follow a minute, and `3시 20초` -- an hour and a second with no
minute between them -- left its `초` outside the span to be read as twenty
seconds.

The two silences that buys are not equally comfortable, and the second should
not be read as the first's twin. `3시 30분` is how a clock time is written, so
silencing `3시 30분 소요` costs a reading few would have wanted. `3시 20초` is
not: nobody writes a time as an hour and a second with the minute left out, so
on that shape the duration reading is the likelier of the two and
`3시 20초 소요` is the more plausible loss. It is still the right trade, because
the alternative was not a silence but a definite wrong sentence -- `3시 20초`
told it states seconds -- and this rule prefers a missing caveat to a false
one. Recorded as accepted rather than as costless.

That the un-nesting can only cost silences is a property of the shape rather
than a corpus result, and the difference matters to whoever re-measures it.
Both tails are optional groups, so removing the nesting can only make a match
longer, never shorter or absent; a longer span can only drop more of what
`_VALUE_MEASUREMENT` found, and dropping more can only remove a reported unit.
So no input exists on which this change adds a caveat -- a measurement showing
"0 gained" is confirming the shape, not sampling for it. Read a future 0 that
way, rather than as evidence that the corpus was wide enough.

Only whitespace joins the components,
and that is what keeps the branch honest: `3시, 30분 소요` ends the clock at the
comma and the thirty minutes beside it are read as the duration they are.

Neither tail carries a lookahead of its own, and two candidates were weighed
rather than one. `(?![가-힣])`, the clock hour's own, costs a wrong sentence:
`3시 30분쯤` then leaves `30분쯤` outside the span, `_UNIT_SUFFIX` reads it as
thirty minutes, and around half past three is reported as a duration.
`(?!(?:간|가량|짜리))`, the day's, costs something too, and the cost is a class
rather than a count: every clock time whose tail wears one of those three
suffixes, on the minute or on the second, is read differently wherever it
appears -- what that then costs is a second question, answered below. Both
tails means both positions the second can take -- `3시 30분간`, `3시 30분가량`
and `3시 30분짜리` on the minute, `3시 30분 20초간` and its two on a second
behind a minute, and `3시 20초간` and its two on a second directly behind the
hour, which only became reachable when the tails stopped being nested.

What it does to them splits in two. Standing alone they gain a caveat where
they were silent, which this guard's bargain allows. Standing BEFORE another
duration they gain nothing -- they take the caveat that is already there and
move it onto the clock's tail, so `3시 30분간 3주` reports `('개월','주')` today
and would report `('개월','분')`, naming the swallowed tail instead of the three
weeks the value is about, and `3시 20초간 3주` would report `('개월','초')` the
same way. Order decides which of the two happens, and not presence:
`korean_measure_unit_mismatch` reports the FIRST same-family unit it finds, so
putting the other duration first moves nothing at all -- `3주 후 3시 30분간`
answers `('개월','주')` either way.

So it is declined on the reading and on that: those tails are likelier to be a
mangled `3시간 30분X` than a duration bound to a minute already inside a clock
time, and the day's tuple was chosen by asking whether `3시X` is ordinary
Korean -- a question about the hour, not about a minute behind one. A swallowed
tail is an accepted silence of the kind this guard already makes, and the day
needs its lookahead for the different reason that `매월 15일간` holds no point in
time at all.

`'21년` is caught where bare `21년` is not, and the apostrophe is the whole of
the difference: it stands in for the elided century and no duration is written
with one. Three characters are read: the straight `'`, the curly `’` that is
the apostrophe proper, and the opening `‘` that a word processor autocorrects a
leading straight quote into -- which is how the character usually arrives.

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
`[0-9]{2,4}` would silence it, and that is the trade this declines. Written with
an apostrophe it is caught, for the reason given above; bare, it is not.

The four-digit year branch is bounded on both sides, and the two bounds do
different work. The left-hand `(?<![0-9])` is what stops a genuine `10000년`
matching on its inner `0000년`. The right-hand `(?![0-9])` stops a four-digit run
that continues into more digits from being read as a year, which only a
contrived value reaches (`2021년12개월`).

The ISO branch earns its place narrowly. An ISO date on its own states no unit,
so the branch does nothing for `2021-03-15`; what it catches is a date with a
unit spelling run onto the end of it, `2021.03.15 일` and `2021-03-15일`, which
without it are read as stating days. It used to be the worst-paying of the four,
because the whole-value rule spent its match on the rest of the value as well
and `2024/01/02 3주` and `2021-03-15 (3일)` went silent with it. Span-local ends
that half of the cost: the quantity overlapping the date is dropped and a
duration standing clear of it is reported, so both are caveated again.

What is left is not the guard's doing but the branch's own reading, and it is
worth stating rather than counting as ended. A quantity written flush onto the
end of an ISO date has its digit run begin inside the date -- `2021-03-153일` is
read by `_VALUE_MEASUREMENT` as `153일`, starting at the date's own `15` -- so
the quantity overlaps the span, straddling its right edge rather than sitting
inside it, and overlap is what drops it. `2021-03-153일`,
`2024/01/023주` and `12.5.33주` say nothing. No reading reports those and still
keeps `2021-03-15일` off the days side, which is the whole of what the branch is
for; they were silent before this change too, so it removes that cost neither
more nor less than it removes any other.

Its year is `[0-9]{2,4}` for the same reason the year+month branch's is, and
holding it at four digits while arguing two-digit years are ordinary notation
one branch above was the contradiction that got it widened: `21.03.15일` and
`25-01-15일` are dates by exactly the premise this file already accepts. Bounded
below at two digits and on the left, so `2.03.15일` and `12021.03.15일` are not
dates. What the branch still misses is a date with no year at all -- `03/15일`
needs two separators to be reached and has one -- which is disclosed in
`korean_measure_unit_mismatch` rather than chased with an alternative of its
own. Reading a slashed month term was tried for #450 and withdrawn: it also
reads the numerator of a small-number rate, and `50/15일` stopped being read.

Widening the year also widened what else looks like a date: a dotted or dashed
numeric triple whose first component is two or three digits reads as one, so
`12.5.3`, `10.1.2` and `10.0.0.1` are points in time as far as this pattern is
concerned. That was a third cost under the whole-value rule, where it silenced
the duration standing beside the version -- `12.5.3 버전, 3주 소요` and
`10.1.2 릴리스, 3주` were caveated before the widening and not after. Span-local
takes the cost back rather than the reading: the triple still matches, and a
version number states no unit of its own, so a duration standing clear of it
-- `12.5.3 버전, 3주 소요`, `10.1.2 릴리스, 3주` -- is reported. Written flush
onto the end of the triple it is not: `12.5.33주` and `10.1.23주` have their
digit run begin at the version's own first digit, so the whole quantity
overlaps the span and is dropped. That is the residue the ISO paragraph above
already records, reached by the same mechanism, and not a second one. A one-digit first component
still falls outside the branch, and that bound no longer separates any of those
values -- what it still separates is `1.2.3 일`, which states days where
`12.5.3 일` does not, and the test re-derives that rather than taking this
sentence for it.

`1500년` and `2000년간` are read as calendar years; both spellings are really
used for durations too, so that reading is honestly ambiguous and this rule
picks the date one.
"""


_SINO_KOREAN_MAGNITUDES = "십백천만억조"
"""The magnitude words `_VALUE_MEASUREMENT_RELAXED`'s number may run through.

The Sino-Korean magnitudes as far as documents use them. This is TWO series
joined, and the join is worth naming because only one of them continues: the
sub-myriad steps 십 백 천, which are 10^1 to 10^3 and stop there because 10^4
has its own word, and the myriad steps 만 억 조, which are 10^4, 10^8 and 10^12
and go on to `경`. Both are enumerable with a defining property rather than
open lexical classes like `_MONTH_WORD_MEMBERS`, so naming them is one decision
rather than a list that grows each round. Reading the six as one run is what
makes the boundary hard to see: it invites looking for a next member after
`조` by the same step that got from `십` to `백`, when what actually continues
is the myriad half, at `경`. `경` is out because a sum of 10^16 has not been
seen in this data; that boundary is stated here so it can be argued with, and
`test_the_magnitude_class_is_a_series_and_this_is_where_it_stops` re-derives
what it costs rather than restating this sentence.

What it costs is narrow, and narrow for a structural reason: the run has to
reach from a digit to the unit without interruption, so it starts at the LAST
digit run and a number escapes when a magnitude outside the class stands
anywhere between that run and the unit. `1경5천조원` is read, because its last
digit run is the `5` and only `천` and `조` stand after it. `1경원`, `1천경원`
and `1억경원` are not, and the `천` in the second of those is in the class and
does not help -- what decides is the whole gap, not any one member of it.

`_DAY_DURATION_SUFFIXES` says an open-ended class needs a safe default and a
tripwire. Only the tripwire is available here, and that is worth saying plainly
because it inverts the usual argument in this file: on THIS scan an
unrecognised member does not cost a caveat, it leaves a wrong sentence
standing, since failing to read the asked unit is what #451 is. There is no
consolation in the default, so the tripwire carries the whole load.

`[만억천조]` was what shipped for #445 -- the myriad steps plus `천`, which is
neither a full series nor a closed one -- and `2백만원`, two million won and as
ordinary in a Korean document as `2천만원`, was told it states `달러` for
exactly that reason. Adding `십` and `백` is what makes the sub-myriad half
complete rather than partial, and completing it is the decision; `십` on its
own buys `5십원` and `2백5십원`, which
`test_the_magnitude_class_is_a_series_and_this_is_where_it_stops` pins so the
member cannot be dropped with the suite green.
"""

_RELAXED_QUANTITY_NUMBER = (
    r"\d[\d,.]*\s*(?:[" + _SINO_KOREAN_MAGNITUDES + r"]\s*)*"
)
"""The number `_VALUE_MEASUREMENT_RELAXED` reads, named so the tests can rebuild
the pattern instead of restating it.

Two of them opened with a copy of this text, and a copy of a pattern is a claim
about the pattern that nothing checks: both went on passing when the number
changed under them for #451, because their probes happened not to distinguish
the old shape from the new one. Naming it is the same remedy
`_UNIT_SUFFIX_MEMBERS` and `_MONTH_WORD_MEMBERS` get, and the probes were
widened at the same time.
"""

_UNIT_SHADOW_WORDS = ("분기", "주년", "년대", "주주", "secondary")
"""Words that a unit spelling only BEGINS, refused where they stand complete.

`분기` is a quarter and its first syllable is the spelling for MINUTE; `주년`,
`년대` and `주주` stand the same way to WEEK, YEAR and WEEK, and `secondary` to
SECOND. `_VALUE_MEASUREMENT_RELAXED` declines to read a spelling where one of
these stands complete in its place, which is what gives `3분기 실적, 2시간 소요`
asked in minutes its `시간` caveat back.

The class is open and takes both things `_DAY_DURATION_SUFFIXES` says an open
class needs. The DEFAULT is the safe direction: a word left off leaves the
reading this scan already made, so it costs a lost caveat rather than a new
sentence, which is what this scan did for every member here before the guard
existed. The TRIPWIRE is
`test_a_shadow_word_is_one_the_reporting_scan_already_refuses`, which refuses a
member whose complete form `_value_measure_units` still reads as a quantity.
`분간` is the shape it catches: with `일간` listed,
`korean_measure_unit_mismatch` answers a `몇 일인가?` about `3일간` with
`('일', '일')`, a caveat naming the asked unit against itself. No count is
written here; the tuple is the list.

Order inside the guard is free, unlike the `달러`-before-`달` constraint the
alternation carries, because a negative lookahead asks only whether SOME
alternative matches where it stands.
`test_a_word_a_unit_spelling_only_begins_is_not_that_unit` carries one fixture
per member and fails if a member is added without one.
"""

_UNIT_SHADOW_GUARD = (
    r"(?!(?:"
    + "|".join(re.escape(w) for w in _UNIT_SHADOW_WORDS)
    + r")(?![가-힣A-Za-z]))"
)
"""`_UNIT_SHADOW_WORDS` as a lookahead standing where the spelling would begin.

It consumes nothing, so every match `_VALUE_MEASUREMENT_RELAXED` makes still
begins at a decimal digit and `_value_states_asked_unit` reads it unchanged.

The inner `(?![가-힣A-Za-z])` is what makes the refusal conditional on the listed
word standing COMPLETE, and it is the whole of the guard's safety in the other
direction, since a listed word is also the prefix of longer strings that stand
in values which do state the asked unit -- `30분기준` is `30분` plus `기준`.
`_VALUE_MEASUREMENT_RELAXED` argues that and
`test_the_shadow_bound_admits_a_digit_and_refuses_a_letter` re-derives why the
class is not `_VALUE_MEASUREMENT`'s `[가-힣0-9A-Za-z]`.

Named rather than inlined because `test_a_unit_suffix_would_be_inert_here` and
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering` rebuild
this pattern, and a copy of a pattern is a claim about the pattern that nothing
checks -- which is what `_RELAXED_QUANTITY_NUMBER` was named for.
"""

_VALUE_MEASUREMENT_RELAXED = re.compile(
    _RELAXED_QUANTITY_NUMBER
    + _UNIT_SHADOW_GUARD
    + r"(?P<unit>"
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
"""The suppression scan: `_VALUE_MEASUREMENT` read for whether a value CARRIES
the asked unit rather than for what it STATES.

How the two differ, and which way each difference runs, is set out in
`_value_states_asked_unit` and deliberately not here -- set out, and not counted
there either. A list in this line is what #453 falsified, by adding a part to
this pattern; the description belongs next to the code that has to be right
about it.

The lookahead is right for deciding what a value STATES -- `2년차` is a second
year of service, not two years -- but wrong for deciding whether the value
already carries the unit that was ASKED for. There it hid the asked unit and let
the caveat fire anyway: `3시간30분` answering `몇 시간인가?` was told the value
states minutes and that no conversion is applied, when the leading quantity is
exactly the hours asked for. A single space changed the outcome, because
`3시간 30분` passes the lookahead and `3시간30분` does not.

The number is wider in three ways, and #451 is what made them necessary. They
are three and not two, which matters below: the magnitude group is a RUN rather
than one character, its CLASS is `_SINO_KOREAN_MAGNITUDES` rather than
`[만억천조]`, and these are independent -- `2천만원` needs only the run, `2백원`
needs only the class, and `2백만원` needs both. So `2천만원`, `1억5천만원`,
`2백만원` and `2백원` are read -- `X천만원`, `X억Y천만원`, `X백만원` and `X백원`
are how a Korean document writes a sum, and `원` is a live question counter, so
a `몇 원인가?` answered any of them was told the value states `달러` beside an
answer whose leading figure is won. The digit class is `\\d`, every Unicode
decimal digit rather than a listed range, because naming a range would leave the
next script out; `verinote.text.nfc` is not `nfkc` and folding compatibility
forms would have this rule compare a value differently from every other
comparison made on it, but admitting the characters in a class local to this
pattern normalizes nothing, so the one-normalizer rule is not in play.

What is still out of reach is stated as a rule and not as a list, because a
list here has been wrong every time it has been written -- three times, each
time by leaving out a class the next reader found. The rule is about a unit
spelling and not about a value, and it is ONE existential condition with two
ways to fail:

    this scan reads a given unit ONLY WHERE SOME DECIMAL DIGIT stands
    before that unit's spelling with nothing between them but digits,
    separators, magnitude words from `_SINO_KOREAN_MAGNITUDES`, and
    whitespace.

Only where, and not wherever. The condition is what every match must satisfy,
so failing it at every digit is what puts a value out of reach -- which is the
whole of the account below, and that direction is exact: every match this
pattern makes begins at a decimal digit and reaches its spelling through those
four character classes and nothing else. It does not run the other way.
`3달러` has a digit before its `달` and an empty gap between them, and is still
not read as months, because the alternation takes `달러` first.
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering` is
where that constraint lives, and it is the one thing a reader cannot get from
the condition above.

That is a mechanism with a bound rather than a case that happened to be found,
and the bound is what keeps this from being a list. A prefix pair can only bite
when the two spellings CROSS CANONICAL UNITS, because the converse asks whether
the unit was read and not which spelling read it: `3주일` satisfies the
condition on its `주` and the alternation takes `주일`, and nothing is lost,
since both are WEEK. `달`/`달러` is the only cross-family prefix pair in the
table, and
`test_달러_is_the_only_prefix_pair_that_crosses_canonical_units` fails the day a
row creates a second one -- so a new spelling cannot quietly widen this
exception.

The other way it does not run is narrower and worth a clause rather than a
paragraph: a separator is admitted only inside the leading digit run and never
after a magnitude word, so `2천만,년` satisfies the condition as worded and is
read as nothing. `_RELAXED_QUANTITY_NUMBER` is the shape, and
`test_a_separator_is_admitted_only_inside_the_leading_digit_run` is the
tripwire: it was written because admitting `[,.]` after the magnitude group
left the whole suite green, so this exception was a reading of the constant
rather than a claim anything would notice losing. Both classes are held now,
which is what makes naming two of them a bound rather than a count.

SOME DIGIT and not EVERY DIGIT, and the difference is the whole sentence.
`1경5천조원` qualifies on its `5`, whose gap to the `원` is `천조`, and does not
qualify on its `1`, whose gap holds the `경`; the scan resumes from the next
start rather than giving up at the first, so the value is read. A reader who
takes the gap clause distributively gets that value wrong -- which is the
mistake an earlier draft of this paragraph made in the code, saying "the FIRST
digit" while a test in the same commit asserted the `5`. Existential is the
property; "reads from the last digit run" is a consequence of the run being
greedy, not the rule.

Say DIGIT and not just "some", because the quantifier has a second axis and
only one of them is this rule. Ranged over OCCURRENCES OF THE SPELLING it
would say every `원` in the value must be reachable, and that is false:
`2천만원 지원 (15,000달러)` holds two, of which only the first is, and the
value is read. `1경5천조원` cannot catch that mistake -- it has one `원` and
comes out true either way -- so the witness does not substitute for naming the
noun. `지원` is this file's own standing example for why the digit requirement
exists, which is how ordinary the wrong axis is.

Failing that condition at every digit puts a unit out of reach, and the groups
below are the ways it is failed. Read that direction only: the groups account
for what the condition excludes, not for everything this scan declines, since
the two classes above are declined while satisfying it. What the direction does
buy is the thing four drafts of this paragraph kept getting wrong -- a cause
that fails the condition needs no new entry here, because the condition already
covers it.

FAILING THE DIGIT. No decimal digit stands before the asked unit, so there is
nothing for the scan to start from. A Sino-Korean numeral (`이천만원`) is the
case this file has recorded longest, but the reachable ones are the native
Korean numerals -- `한 시간 30분` asked in hours, `두 달 3주`, `이틀 3주` --
and the quantities that carry no numeral at all: `반년`, `수개월`, `수십억원`,
`여러 달`. `반년` is why the condition cannot be phrased as "a numeral the scan
cannot spell": there is no numeral in it to fail to spell. `한 시간 30분` is
the most reachable of all of them, since `일 시간` is not Korean and an hour
and a half is ordinarily written that way -- though `1시간 30분` and `1.5시간`
are read, so what is out of reach is the notation and not the quantity.

FAILING THE GAP. Digits do stand before the unit, and EVERY one of them has
something in its gap: a magnitude outside the class (`1경원`, and `1천경원`,
whose in-class `천` does not save it -- there is no later digit to start from),
or an approximator (`3천만여원`, `20여년`). This is where the quantifier earns
its keep: one blocked digit proves nothing, since `1경5천조원` has a blocked
`1` and is read anyway. Position and not vocabulary -- the same `여` AFTER the
unit spelling never enters a gap, so `3개월여` and `2년여` read normally.

All of these are pre-existing rather than introduced by #451. That change
widened the gap condition, and it widened the digits themselves from `[0-9]`
to every Unicode decimal digit; what it left alone is the requirement that
there be a decimal digit at all, which is the whole of the first group.
`korean_measure_unit_mismatch` records what they cost.

The run takes no digits between its magnitude words, and that is measured rather
than assumed. `(?:[...]\\s*[\\d,.]*\\s*)*` reads `1억5천만원` from the `1` where
this reads it from the `5`, and both report KRW: a stacked number is a digit
run, then magnitude words possibly separated by further digit runs, then the
unit, so where there are inner runs this pattern starts at the last of them and
reaches the same unit. What that rests on is that no spelling in
`_MEASUREMENT_UNIT_SPELLINGS` begins with a magnitude character, so the region
the longer form swallows holds no unit to hide.
`test_the_magnitude_run_needs_no_inner_digits` re-derives the premise from the
live table and the equality from a corpus, and shows the two patterns really do
differ, since their match starts do.

Neither widening can put a unit in front of a reader, and that is a theorem
rather than a corpus result. This pattern's one caller is
`_value_states_asked_unit`, whose one output is the early return in
`korean_measure_unit_mismatch`, so the only way a wider number changes an answer
is by reading FEWER units. It cannot: the number's own language is
`[\\d,.\\s십백천만억조]` and no spelling begins with a character in it, so no
spelling can start inside a number and no number can extend across one, and at
every start where the narrower pattern matched this one matches the same span
and unit. `test_the_widened_number_can_only_silence` asserts that premise as
well as the outcome, so the argument degrades loudly rather than silently if the
table ever gains such a spelling.

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

What no ordering buys is a spelling that is only the head of an unrelated word,
and `_UNIT_SHADOW_WORDS` is the part of that this scan can settle. The rule is
necessity only:

    this scan reads a spelling as a unit ONLY WHERE the text there does not
    begin with a word in `_UNIT_SHADOW_WORDS` standing COMPLETE -- complete
    meaning no Hangul or Latin letter continues it.

`3분기 실적, 2시간 소요` asked in minutes is the witness: it named `시간` before
this scan existed, said nothing while the guard was absent, and names `시간`
again.

Complete is the whole of the second half, and dropping it inverts the rule's
sign, because a listed word is also the prefix of longer strings that a real
value writes: `30분기준` is `30분` plus `기준`, `10년대출` is `10년` plus `대출`,
`2주주기` is `2주` plus `주기`. Each has a listed word standing exactly where the
spelling does and each does state the asked unit, so an unbounded refusal takes
`30분기준 회의, 2시간 소요`, `10년대출 상환, 3개월 준비` and
`2주주기로 반복, 3개월` and turns a CORRECT SILENCE into a wrong sentence -- not
a lost caveat, since a value that states the asked unit had no caveat to lose.
That is the failure the boundary shapes weighed for this scan were rejected for,
arrived at from the other side, and it is the opposite direction from the one an
unlisted word costs.

The class is `[가-힣A-Za-z]` and not `_VALUE_MEASUREMENT`'s `[가-힣0-9A-Za-z]`,
and the difference is load-bearing rather than an oversight: a digit after a
listed word begins a new number, it does not continue a word.
`80년대2000년대 비교, 3개월` asked in years names its `개월` under this class and
is silent under the other, and the same goes for `3분기4분기 실적, 2시간 소요`.
The two lookaheads are asking different questions at different positions, and
`test_the_shadow_bound_admits_a_digit_and_refuses_a_letter` re-derives the cost
of merging them.

The list is open and takes both things `_DAY_DURATION_SUFFIXES` says an open
class needs, and the DEFAULT is what decides the shape. A word left off leaves
the reading this scan already made, which is a lost caveat and not a new
sentence, so an unlisted member costs nothing new; a boundary placed on the UNIT
instead has the opposite default, since josa is written flush against the noun
and `3주의 준비, 2개월 소요` would then be caveated against a value that does
state three weeks. The tripwire is
`test_a_shadow_word_is_one_the_reporting_scan_already_refuses`: a member whose
complete form the reporting scan still reads as a quantity puts the two scans on
different readings of one value, and `korean_measure_unit_mismatch` then answers
`('일', '일')`. No count is written here; the tuple is the list.

#451 widened the number and thereby widened every silence this scan already
makes; that is the cost of the change rather than a separate defect. What
reaches further is not a list of values but the three notations named above --
a magnitude run, a sub-myriad magnitude, and a non-ASCII decimal digit -- so
anything this scan already read wrongly it now reads wrongly in those too. The
guard above travels with them, because it is placed on the spelling and not on
the number: `２분기 실적, 2시간 소요`, `１주년 기념, 3개월 준비` and
`８０년대 후반, 3개월` are the full-width twins of the three Korean witnesses and
are caveated for the same reason they are. What stays silent is recorded one
notation at a time: `2천만주 보유, 3개월 준비` needs the run,
`3백주 보유, 3개월 준비` needs
the sub-myriad magnitude, and `１００주 보유, 3개월 준비` needs the digit class --
each silent under the shipped number and firing under exactly the one narrowing
that names it, which
`test_caveats_lost_to_the_suppression_scan_are_recorded_not_fixed` re-derives
rather than taking this sentence for. Those three are the `100주` shares
reading, which #467 carries, each held silent by a different part of the number
#451 widened rather than by how large it is. Notation and not quantity:
`20000000주`, `300주` and `100주` write those same three amounts in plain ASCII
digits and the pre-#451 number reads the `주` in every one of them, so what the
diagonal separates is which notation reaches each row and not which row is
bigger. A holding really is written `2천만주`, so that one is the most
reachable. The
point-in-time silences travel the same way, since this scan does not consult
`_TIME_POINT`:
`２０２１년 착수, 총 3주` asked in years and `１５일 마감, 3주` asked in days are
silent now, where their ASCII spellings were already silent, and
`test_a_point_in_time_silence_travels_into_the_new_notations` holds that half
because it is not this class. Read none of these as a set. The rule is that the
three notations join every reading this scan already makes, and a count here
would be a count of an open class -- which is what the earlier draft of this
paragraph got wrong by saying two, since a rule that omits a notation prices
none of the values only that notation reaches.

One reading in the other direction arrived with them: `３시간30분` asked in
`몇 시간인가?` was told it states minutes and is silent now, which is the defect
of the paragraph above this one, reaching a notation it could not before.
"""


def _value_states_asked_unit(value: str, asked_unit: str) -> bool:
    """Whether the value carries a quantity in the unit the question asked for.

    Read with `_VALUE_MEASUREMENT_RELAXED`, which does not read what
    `_value_measure_units` reads, and the differences run in both directions. No
    count is written here, because a count is what goes short the next time one
    is added -- #453 added one. What the differences are is a diff of the two
    patterns and of the two callers, not a number in this line.

    It reads MORE where the NUMBER is wider -- any Unicode decimal digit rather
    than ASCII, a run of magnitude words rather than one, a wider magnitude class
    -- and where the trailing lookahead is absent. That those widenings read more
    and never less is argued from the character sets rather than sampled; the
    argument is in `_VALUE_MEASUREMENT_RELAXED` and its premise is asserted by
    `test_the_widened_number_can_only_silence`. It is worth knowing which of the
    two the tests are doing, because `finditer` resumes from a match's end and a
    longer match could in principle hide a later one: the premise closes that,
    and the corpus sweep confirms it.

    It reads LESS where `_UNIT_SHADOW_GUARD` refuses a spelling that a word in
    `_UNIT_SHADOW_WORDS` stands complete in place of, which is #453.

    And the comparison is with a FUNCTION and not only with that pattern, so it
    differs where the function does: `_value_measure_units` drops a quantity
    overlapping a `_TIME_POINT` span and this scan does not consult that guard,
    so `_value_states_asked_unit("매월 15일 소요", "DAY")` is True where
    `_value_measure_units("매월 15일 소요")` is empty. `_TIME_POINT` states that
    difference from its own side.

    Every unit `_value_measure_units` reports is still caught here, so this still
    subsumes the plain equality test it replaced -- but the narrowing means that
    no longer follows from the character sets, and it now rests on the criterion
    a member of `_UNIT_SHADOW_WORDS` has to meet: the member's complete form is
    one the REPORTING scan reads as no quantity at all, so where the guard
    refuses there was no reported reading to subsume.
    `test_a_shadow_word_is_one_the_reporting_scan_already_refuses` is where that
    criterion is enforced and
    `test_the_suppression_scan_sees_everything_the_value_scan_sees` is where the
    subsumption itself is swept. A member breaking the criterion reddens both,
    which is measured rather than assumed -- `일`, `주`, `일간` and `분간` each
    do.
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

    Empty for a value stating no quantity. A value carrying a point in time is
    read, and the quantities that OVERLAP the point's own span are the ones
    dropped -- overlap and not containment, which `_TIME_POINT` argues and
    `2021-03-15일` witnesses, since a quantity can begin inside a span and end
    outside it. `_value_measure_units("매월 15일 소요")` is empty because the
    `15일` is the day of the month, while `_value_measure_units("매월 15일, 3주")`
    reports the three weeks. `_TIME_POINT` states the rule and what it costs.

    NFC because a value written in NFD spells `년` as two code points while the
    alternation is composed, and casefold because the Latin spellings in the
    table are lower-case. One consequence is worth naming: the spelling handed
    back is the folded one, so a value stating `3 Weeks` is reported as stating
    `weeks`.
    """
    folded = nfc(value).casefold()
    points = [point.span() for point in _TIME_POINT.finditer(folded)]
    return tuple(
        (_MEASUREMENT_UNIT_SPELLINGS[match.group("unit")], match.group("unit"))
        for match in _VALUE_MEASUREMENT.finditer(folded)
        if not any(
            match.start() < point_end and point_start < match.end()
            for point_start, point_end in points
        )
    )


def korean_measure_unit_mismatch(question: str, value: str) -> tuple[str, str] | None:
    """The (asked counter, stated unit) a unit caveat should name, or None.

    A mismatch only when the value states a unit in the same family as the one
    asked for and a second scan finds no quantity in the asked unit itself.

    Both halves are what a pattern reads, not what the value contains, and the
    sentence has to be put that way round: `_value_states_asked_unit` re-reads
    the value for the asked unit with a wider quantity shape and no TRAILING
    lookahead, and anything that scan cannot see is not suppressed on. It reads `6개월` in
    `2년 6개월`, so a `몇 개월인가?` is suppressed, and since #451 it reads the
    won in `2천만원 (15,000달러)` and `2백만원 (15,000달러)` and the years in
    `３년 30주` as well.

    What it still cannot see is given as a rule in
    `_VALUE_MEASUREMENT_RELAXED`, and the rule is the thing to read, because
    every attempt to write the residue as a list has left something out. The
    scan reads a unit ONLY where SOME decimal digit stands before that
    unit's spelling with nothing between them but digits, separators, class
    magnitudes and whitespace -- necessary and not sufficient, since the
    alternation taking `달러` before `달` can refuse a unit that satisfies it.
    Some DIGIT and not every digit, or `1경5천조원`
    reads wrong: it qualifies on its `5` and not on its `1`. The noun is worth
    repeating because ranged over occurrences of the SPELLING the same words
    say something false -- `2천만원 지원` holds two `원` and is read on the
    first. So `한 시간 30분` and `반년 3주`
    fail for want of any digit -- as `이천만원` does, and the native-numeral and
    no-numeral forms are the reachable members of that class rather than the
    Sino-Korean one -- while `1경원`, `3천만여원` and `20여년` have digits whose
    gaps are all blocked. The other half of the residue is a spelling
    `_MEASUREMENT_UNIT_SPELLINGS` leaves out on purpose: the `개년` in
    `5개년 계획 3주` and the bare `월` in `6월 및 30주`. #451 widened what may
    stand in a gap, and widened which characters count as digits; the
    requirement that there be a digit at all is older than it and survives it,
    and the bullet below is where the survivors live.

    The two halves are read by different patterns, and that is deliberate rather
    than an oversight. What the value STATES comes from `_value_measure_units`,
    which refuses a unit run into the next character and reads ASCII digits and
    at most one of `[만억천조]`. Whether the value CARRIES the asked unit comes from
    `_value_states_asked_unit`, which differs from it in both directions and
    sets those differences out. Add a part to either pattern and that is the
    paragraph to correct: it reads the pattern and sits beside it, which is why
    the description lives there and not here. With one pattern
    doing both, the same lookahead that correctly declines to read `2년차` as two
    years also hid the asked unit in `3시간30분` and `2년6개월`, and the caveat
    fired on a value whose leading quantity was exactly what the question asked
    for. Spacing decided it, which no reader would predict; #451 was the same
    defect one layer down, where the notation the number was written in decided
    it instead.

    The divergence is permitted in one direction, and the rule is about READING
    MORE rather than about every change to these patterns. Reading more with the
    relaxed pattern ends caveats and cannot start one, since it feeds a single
    early return here; reading more with `_value_measure_units` is what a reader
    is shown, so it adds sentences and is a separate change with a sweep of its
    own.

    Reading LESS with the relaxed pattern is the case an unscoped version of that
    sentence was read as covering, and it runs the other way: it STARTS caveats.
    `3분기 실적, 2시간 소요` asked in minutes is silent without
    `_UNIT_SHADOW_GUARD` and names its `시간` with it, which is the whole of #453.
    So a narrowing here cannot be cleared by a witness -- a witness shows one
    caveat gained and says nothing about the ones gained beside it -- and what
    #453 rests on instead is the `CONTINUED` cell of
    `test_the_shadow_guard_gains_no_caveat_on_a_word_it_only_prefixes` being
    empty, which is a claim about a population rather than about a value.

    The first same-family unit is reported, not the first unit. `30% 완료, 3주`
    asked in months states a ratio first and a duration second, and the duration
    is the part the question was about.

    The main causes of an accepted silence, rather than all of them: a value
    stating no number; a unit run into the next syllable (`2년차`); a quantity
    that overlaps a point in time (`매월 15일`); a spelling outside the
    table; a suffix outside `_UNIT_SUFFIX`;
    a number that stacks magnitude words (`2천만원`) or uses one outside
    `[만억천조]` (`2백원`), since `_VALUE_MEASUREMENT` admits at most one and
    only from those four -- `2백만원` needs both allowances and is silent for
    either reason; and a number written in full-width digits (`３년`),
    which its `[0-9]` does not admit and `nfc` does not fold away. `nfkc` would
    fold it, but `verinote.text.nfc` is the one normalizer the rest of the
    codebase compares through, and folding compatibility forms here alone would
    have this rule read a value differently from every other comparison made on
    it. A non-breaking space between the number and the unit is fine
    (`3<NBSP>년` states years), so
    this silence is specifically the digits. Those last two are silences on the
    REPORTING side only since #451. The suppression scan reads all three
    notations, so where the ASKED unit is the one written that way the whole rule
    now says nothing instead of naming a neighbour: `2천만원 (15,000달러)` asked
    in won, `３년 30주` asked in years. Asked in some other unit of the family the
    quantity is still unreported and a neighbour can still be named --
    `３년 30주` asked in months says `주` -- which is the reporting silence doing
    what it has always done. An earlier cross-family quantity
    does not silence a later same-family one.

    One silence is worth separating from those, because it is the only one where
    the value did earn a caveat and this rule loses it by misreading rather than
    by not reading. `_value_states_asked_unit` refuses a spelling where a word in
    `_UNIT_SHADOW_WORDS` stands complete in its place, so `3분기 실적, 2시간 소요`
    asked in minutes names its `시간` again. Three residues are left, and none of
    them is a remainder of the others. A spelling that means the other thing with
    NOTHING appended has no longer word to list at all -- `3백주 보유, 3개월 준비`
    is three hundred shares and is silent still, and #467 is where that half is
    filed. A longer word the tuple does not hold is reachable and simply absent:
    `3분야 검토, 2시간 소요` and `3 secondaries, 2 minutes`. And a listed word
    continued flush by another letter is refused refusal on purpose --
    `3분기실적, 2시간 소요` stays silent, which is the price of not caveating
    `30분기준 회의, 2시간 소요` against a value that does state thirty minutes.
    `_VALUE_MEASUREMENT_RELAXED` carries the rule and the two defaults it rests
    on.

    Wrong sentences this is known to produce. These are the ones that have been
    found, not a bound on what exists, and each round of review has added to
    them; read the list as open. They also have no single cause -- a two-digit
    year is not a lexical ambiguity and `second` is not Korean -- so do not
    generalise from it to decide whether some new input is safe.

    * A day of the month with no month term in front of it, in the sense
      `_TIME_POINT` defines one. `15일 마감` is the deadline
      on the fifteenth and is told it states `일`, fifteen days. Nothing in the
      value separates it from `15일 소요`, which really is fifteen days -- only
      the trailing noun does, and that noun does not partition, since `마감`
      heads both readings. A 마감일 relation holding a bare `N일` is ordinary
      contract data, which makes this the most reachable one found so far. It
      reaches further since
      #452: a bare day is a point in time no `_TIME_POINT` branch defines, and
      the whole-value guard used to silence it whenever anything else in the
      value happened to be a date, so `2021년 계약, 15일 마감` was silent and now
      states `일`. The misreading is the same one; only its reach changed.
    * A date written with no year. The ISO branch needs two separators, so
      `03/15일` has one and is reported as stating `일`. Reading a slashed month
      was tried for #450 and withdrawn: `[0-9]{1,2}\\s*/` also reads the numerator
      of a small-number rate, so `50/15일` and `10/30일` stopped being read at all
      and `50/15일 3주` lost a genuine caveat. A dotted form is worse, since
      `1.5일` is a decimal duration with the same shape, and a dashed one collides
      with the range `10-15일`.
    * A value whose asked-unit quantity the suppression scan cannot read, beside
      a same-family unit the reporting scan can, so the caveat names the second.
      What makes the two kinds below worth telling apart is that one is a
      pattern's reach and the other is a table's contents; the split is not a
      claim that there are two of anything, and the NUMBER side is itself given
      as a rule rather than a list, for the reason
      `_VALUE_MEASUREMENT_RELAXED` gives.

      The NUMBER, in the two ways `_VALUE_MEASUREMENT_RELAXED`'s rule can
      fail. That rule is existential -- SOME digit before the unit with a clean
      gap -- so both failures below are failures at every digit, not at one.
      These are the values the rule EXCLUDES; the rule holds only in that
      direction, and the two classes it declines while satisfying are recorded
      with it rather than here.

      No DIGIT at all before the asked unit, so the scan has nothing to start
      from. `이천만원 (15,000달러)` asked in won reports `달러`, and so do
      `한 시간 30분` asked in hours, `두 달 3주` and `이틀 3주` asked in their
      own units, and `반년 3주`, `수개월 3주` and `수십억원 (15,000달러)`,
      which carry no numeral to spell at all. `한 시간 30분` is the one to
      weigh: it is the very sentence this scan exists to prevent, on the way an
      hour and a half is ordinarily written, since `일 시간` is not Korean.
      Notation and not quantity -- `1시간 30분` and `1.5시간` say the same thing
      and are both silent -- which is what makes it a spelling defect rather
      than a limit on what can be asked.

      Something in the GAP between that digit and the unit. A magnitude outside
      `_SINO_KOREAN_MAGNITUDES` (`1경원 (15,000달러)`), or an approximator
      (`3천만여원 (15,000달러)`, `20여년 3주`) -- position and not vocabulary,
      since the same `여` after the unit is harmless.

      #451 moved the gap condition and part of the digit one: it widened what
      magnitudes may stand in the gap, and widened the digits themselves from
      `[0-9]` to every Unicode decimal digit. What it did not touch is the
      requirement that the starting character be a DECIMAL DIGIT at all, which
      is why the whole first group above is older than #451 and survives it. It
      retired `2천만원 (15,000달러)` and `３년 30주` from this bullet, which is
      what it listed before; `1억5천만원 및 20,000달러` was in the test table
      rather than here, and `2백만원 (15,000달러)` was in neither, since this
      change is what found it.

      The SPELLING: a row `_MEASUREMENT_UNIT_SPELLINGS` excludes on purpose.
      `5개년 계획 3주` asked in years reports `주`, and `6월 및 30주` asked in
      months reports `주`, for the reasons given beside those exclusions.
      Reading either on the suppression side was measured against #451 and
      declined. Bare `월` silences `3월 15일간`, `3월 내 15일 소요` and
      `3월 계약 15일 소요` among others, each of which states a duration some
      test asserts is caveated -- the word-month forms beside them,
      `전월 대비 3일 단축` and `매월 15일간`, are untouched, since no digit
      stands before their `월` -- and declining a `월` inside a `_TIME_POINT`
      span does not even reach the two
      the year+month branch's bounds are placed for, since neither `2년 3월` nor
      `10000년 3월` matches that pattern at all. `개년` silences a `5개년` the
      table calls the name of a plan rather than a duration, which is the
      judgement the row was excluded for.

      #451 states the general form these are instances of: an accepted silence
      here is a latent wrong sentence, and a readable same-family unit standing
      beside it is what turns it into one. The form reaches past this bullet --
      `₩20,000,000 (15,000달러)` asked in won reports `달러` for a third reason
      again, the symbol standing where no row in `_MEASUREMENT_UNIT_SPELLINGS`
      can reach it.
    * A bare two-digit year. `21년 3월` is read as a date and so is `'21년`, but
      `21년` alone is left reading YEAR, so `몇 개월인가?` answered `21년` is told
      it states years. Twenty-one years is a real duration, and without the
      apostrophe or a month beside it nothing here separates the two readings.
      As with the bare day above, #452 widened the reach rather than the
      misreading: `2021년 착수, 21년` was silenced by the four-digit year
      standing next to it and now states `년`.
    * The second member of a date or clock list whose head is elided. Korean
      writes two days of one month as `3월 15일~20일` or `매월 15일, 30일`, and
      the branch takes the first member only, so the second stands outside every
      span and is read as a quantity: both of those are told they state `일`, and
      so are `3월 15일-20일`, `3월 15일·20일`, `3월 15일 및 20일`, `3월 15일과 20일`,
      `3월 15일 또는 20일`, `3월 15일(월), 20일(토)`, `매월 10일 / 25일`,
      `다음 달 1일, 15일`, `3월 초 5일, 15일` and `매월 5일, 15일, 25일 지급`.
      `2021년, 22년` is the same shape on the year and `3시 30분 ~ 45분` on the
      clock. Each was silent before #452, because the whole-value guard spent
      its match on the second member as well.
      `3월 15일부터 20일까지` is silent, and that is a spacing accident rather
      than a rule -- the trailing lookahead refuses `20일까지`, and the spaced
      `3월 15일 부터 20일 까지` is caveated.
      Unlike the bare day above there IS a month term left to read, so this one
      is reachable in principle and is filed rather than accepted. Two
      candidates were measured. A bounded tilde continuation on the day reaches
      `~` and none of the other separators, and that set is an open lexical
      class. Suppressing a leaked quantity whose unit a span already states
      reaches every separator, but only where the head's own component survives
      the trailing lookahead -- `3월 15일, 20일` yes, `3월 15일과 20일` no -- and
      reaches no ISO or slashed head, whose spans state no unit to repeat. It
      would also silence `매월 15일 마감, 3일 이내 지급` and `3시 30분 회의, 30분 연장`,
      which state a genuine duration that happens to share a unit with the date
      beside it.
    * An English ordinal. `second` is in the spellings table as a unit, so
      `2 second review` is reported as stating `second`. The neighbouring
      `3 secondary reviews` is silent for an unrelated reason -- Latin continues
      past the spelling and the lookahead refuses it -- which does not reach the
      spaced form. The row stays, because `30 seconds` answering `몇 분인가?` is
      a real result and the question side can only ask SECOND through `몇 초`.
      Since #453 the suppression scan refuses `secondary` too, so
      `3 secondary reviews, 2 minutes` asked in `몇 초` names the minutes.
      `3 secondaries, 2 minutes` does not: `secondary` is not a prefix of
      `secondaries`, so the word is simply absent from the tuple. The reporting
      reading of the spaced `2 second review` is untouched, which is what keeps
      this bullet here.
    * `몇 년인가?` answered `100주` (one hundred shares), and `몇 시간인가?`
      answered `5분` (five people, honorific): Korean spellings that mean two
      things, read here as the unit. These two also need a question asked in a
      unit the relation does not really measure.
      This is also the residue of the syllable class
      `_VALUE_MEASUREMENT_RELAXED` records, in its silent form:
      `3백주 보유, 3개월 준비` and `2천만주 보유, 3개월 준비` asked in weeks are
      the same shares reading producing a lost caveat instead of a sentence.
      #467 carries both forms.
    * The day itself, in a value where a free word rather than a bound suffix
      marks the duration. The day branch consults only what is written flush
      against `일`, so `매월 15일간` is read as a duration while `매월 15일 동안`
      is a day of the month with a word after it, and the fifteen days it really
      states are never reported. `남짓`, `내내`, `이상` and any other free word
      implying a span behave alike, and the class is not bounded by that list --
      "means a duration" is open, which is why the branch keys on being bound
      instead. #458 tracks moving the lookahead's position, which is what would
      reach these; listing the words would not, because standard orthography
      spaces them.
      Two mechanisms produce that one silence and they are worth telling apart,
      since conflating them is what this file keeps having to correct. In
      `매월 15일 동안` the `15일` IS read by `_VALUE_MEASUREMENT` and is then
      dropped for overlapping the day's span; in `매월 15일동안` no quantity is
      read at all, because the trailing lookahead refuses a unit run into
      Hangul. Same outcome, different cause, and only the first would move if
      the span moved.
      What #452 did change is the rest of such a value. The guard used to be
      whole-value, so a day read as a date silenced every other quantity beside
      it and `매월 15일동안 3주` said nothing about its three weeks; span-local
      reports them. The day is still lost either way, so #458 is narrowed rather
      than closed.
    * A quantity overlapping a point-in-time expression. `매월 3일 소요` is the
      third of each month and states no duration this rule can see, so a
      `몇 주인가?` against it is silent; the same goes for the `15일` in
      `3월 중 15일` and the `30분` in `3시 30분`. This is the guard working rather
      than failing, and it is listed here because the reading is a judgement:
      `매월 3일 소요` can be read as three days a month, and `_TIME_POINT` argues
      for the other reading. What is no longer true is that the loss spreads --
      #445 and #450 made it whole-value, so any duration anywhere else in the
      value went with it, and #452 confined it to the span.

    None of these is fixed here. #445 asks that a verified answer in another unit
    be caveated, not that every ambiguous spelling be resolved; #450 added the
    point-in-time guard an earlier version of this paragraph asked for, and #452
    made it span-local, and neither changed that. Two of the entries above are
    beyond any widening of `_TIME_POINT` for the same reason: in `15일 마감` and
    in `21년` the notation for a point in time is identical to the notation for a
    quantity, so there is nothing left in the value to read. Do not generalise
    that to the rest of the list -- the elided second member has a month term
    standing right in front of it, and what has stopped it being fixed is that
    every rule tried so far reaches some spellings and not others.
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
