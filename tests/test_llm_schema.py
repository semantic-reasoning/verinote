# SPDX-License-Identifier: MPL-2.0
import copy
import dataclasses
import importlib.util
import pathlib
import sys

import pytest

from verinote.llm import LLMError
from verinote.llm import schema as llm_schema
from verinote.llm.schema import (
    EXTRACTION_SYSTEM,
    FACT_OBJECT_SCHEMA,
    QUERY_INTENT_CONJUNCTIVE_HOP_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_INTENT_TARGET_SCHEMA,
    parse_facts,
)
from verinote.pipeline import query_intent
from verinote.pipeline.query_intent import (
    QUERY_INTENT_BLANK_NULLABLE_FIELDS,
    QUERY_INTENT_COMPARISON_DOMAINS,
    IntentTarget,
    QueryIntent,
    QueryIntentKind,
    _blank_nullable_fields,
    _comparison_domains,
    _nullable_string_fields,
    parse_query_intent,
)


def _synthetic_intent_schema():
    """QUERY_INTENT_SCHEMA plus two nullable string properties it has never had.

    A hand-written name list and a schema derivation agree on today's contract, so
    only a schema the module has never seen tells them apart: the derivation
    answers with `unit` and `note`, the list cannot.
    """
    schema = copy.deepcopy(QUERY_INTENT_SCHEMA)
    schema["properties"]["unit"] = {"type": ["string", "null"], "enum": ["kg", "m", None]}
    schema["properties"]["note"] = {"type": ["string", "null"], "minLength": 1}
    schema["required"] = list(schema["properties"])
    return schema


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
    # Every property stays in `required`, `reason` included. OpenAI structured
    # outputs run this schema with `strict: True` (openai_adapter.py), which
    # forbids both optional properties and per-kind conditional requirements.
    # Dropping `reason` from `required` to express "only unknown_or_unsupported
    # needs a reason" would silently break that adapter; the reason contract is
    # carried by the prompt and the intent validator instead (issue #237).
    assert QUERY_INTENT_SCHEMA["required"] == list(QUERY_INTENT_SCHEMA["properties"])
    assert "datalog" not in QUERY_INTENT_SCHEMA["properties"]
    assert QUERY_INTENT_SCHEMA["additionalProperties"] is False
    assert (
        "discover_entity_relations"
        in QUERY_INTENT_SCHEMA["properties"]["kind"]["enum"]
    )
    assert "count" not in QUERY_INTENT_SCHEMA["properties"]["kind"]["enum"]
    hop = QUERY_INTENT_CONJUNCTIVE_HOP_SCHEMA
    assert hop["required"] == ["subject", "relation", "object"]
    assert hop["additionalProperties"] is False
    assert hop["properties"]["relation"]["type"] == "object"
    assert hop["properties"]["relation"]["properties"]["kind"]["enum"] == ["relation"]
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


def test_parse_query_intent_accepts_valid_lookup_subject_and_lookup_relation():
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


def test_parse_query_intent_rejects_count_until_planner_support_exists():
    with pytest.raises(LLMError, match="unknown query intent kind: count"):
        parse_query_intent(
            _intent_payload(
                kind="count",
                relation={"kind": "relation", "value": "참여"},
                reason=None,
            )
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


def test_parse_query_intent_accepts_entity_relation_discovery():
    broad = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            reason=None,
        )
    )
    direct_first = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation={"kind": "relation", "value": "provides"},
            reason=None,
        )
    )
    candidate_first = parse_query_intent(
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation_candidates=["provides", "connects"],
            reason=None,
        )
    )

    assert broad.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert broad.subject == IntentTarget("entity", "Sample Entity")
    assert broad.relation_candidates == ()
    assert direct_first.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert direct_first.relation == IntentTarget("relation", "provides")
    assert candidate_first.kind == QueryIntentKind.DISCOVER_ENTITY_RELATIONS
    assert candidate_first.relation_candidates == ("provides", "connects")


def test_parse_query_intent_accepts_unsupported_with_reason():
    intent = parse_query_intent(
        _intent_payload(reason="requires planning")
    )

    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
    assert intent.reason == "requires planning"


_REASON_TOLERANT_INTENTS = [
    (
        "lookup_object",
        {
            "subject": {"kind": "entity", "value": "샘플인물"},
            "relation": {"kind": "relation", "value": "역할"},
        },
    ),
    (
        "lookup_subject",
        {
            "relation": {"kind": "relation", "value": "역할"},
            "object": {"kind": "entity", "value": "샘플직책"},
        },
    ),
    (
        "lookup_relation",
        {"subject": {"kind": "entity", "value": "샘플인물"}},
    ),
    (
        "discover_entity_relations",
        {"subject": {"kind": "entity", "value": "Sample Entity"}},
    ),
    (
        "compare_typed_value",
        {
            "subject": {"kind": "entity", "value": "샘플항목"},
            "relation": {"kind": "relation", "value": "수량"},
            "operator": ">",
            "value_type": "number",
            "value": "10",
        },
    ),
]


@pytest.mark.parametrize(
    ("kind", "fields"),
    _REASON_TOLERANT_INTENTS,
    ids=[kind for kind, _ in _REASON_TOLERANT_INTENTS],
)
def test_parse_query_intent_accepts_advisory_reason_on_every_kind(kind, fields):
    """A reason on a well-formed non-unknown intent is advisory, not a violation.

    The schema requires every property, so a provider that fills `reason` while
    classifying correctly used to have its whole answer rejected as off-schema
    (issue #237). Only `unknown_or_unsupported` reads `reason`, so tolerating it
    elsewhere cannot change the resulting query.
    """
    intent = parse_query_intent(
        _intent_payload(kind=kind, reason="classified from the question wording", **fields)
    )

    assert intent.kind == QueryIntentKind(kind)
    assert intent.reason == "classified from the question wording"


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_query_intent_treats_a_blank_nullable_string_as_null(blank):
    """A blank `reason` means "no reason", exactly like an omitted key.

    `claude_cli_adapter` renders the schema into the prompt rather than
    constraining decoding, so `minLength: 1` is never enforced -- and the schema
    demands the `reason` key while the prompt says to leave it null. A model
    splitting that difference with `""` is routine, and rejecting it would kill a
    correctly classified question for the very reason issue #237 did.
    """
    intent = parse_query_intent(
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "샘플인물"},
            relation={"kind": "relation", "value": "역할"},
            reason=blank,
        )
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.reason is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_query_intent_still_treats_a_blank_value_as_null(blank):
    """`value` has no enum, only `minLength: 1`, so blank still means absent.

    The schema pins no domain for `value`, so `""` carries no meaning a strict
    provider could not have expressed as null -- the same situation as `reason`.
    Only the enum fields lose the blank-to-null reading.
    """
    intent = parse_query_intent(
        _intent_payload(
            kind="lookup_object",
            subject={"kind": "entity", "value": "Acme"},
            relation={"kind": "relation", "value": "CEO"},
            value=blank,
            reason=None,
        )
    )

    assert intent.value is None


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("field", ["operator", "value_type"])
def test_parse_query_intent_rejects_a_blank_enum_field_on_a_non_compare_intent(field, blank):
    """Blank is not null for a field the schema constrains with an enum.

    `reason` and `value` are plain nullable strings, so a blank one is just how a
    prompt-only provider spells the null the schema forces it to emit. `operator`
    and `value_type` are different: their enums list every admissible non-null
    value, and `""` is not among them. Normalising blank to null there would let
    an off-schema value through on the six kinds that ignore these fields, which
    puts the validator's boundary outside QUERY_INTENT_SCHEMA's -- exactly what
    `test_parse_query_intent_rejects_off_schema_comparison_fields_on_every_kind`
    forbids for `"contains"`.
    """
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="lookup_object",
                subject={"kind": "entity", "value": "Acme"},
                relation={"kind": "relation", "value": "CEO"},
                reason=None,
                **{field: blank},
            )
        )


def test_query_intent_blank_nullable_fields_are_the_schema_fields_without_an_enum():
    """Which fields treat blank as null is read off the schema, not listed by hand.

    The rule is "a field whose schema pins a domain does not get to spell null as
    blank". Deriving the set from QUERY_INTENT_SCHEMA keeps that rule true if the
    schema later adds an enum to a field (or drops one), instead of leaving a
    hand-written name list to drift out of the contract adapters are handed.
    """
    assert QUERY_INTENT_BLANK_NULLABLE_FIELDS == frozenset(
        name
        for name in _nullable_string_fields(QUERY_INTENT_SCHEMA)
        if "enum" not in QUERY_INTENT_SCHEMA["properties"][name]
    )
    assert QUERY_INTENT_BLANK_NULLABLE_FIELDS == frozenset({"value", "reason", "answer_var"})


def test_nullable_string_fields_follow_a_schema_it_has_never_seen():
    """A property added to the schema alone must show up without a code change.

    That is the whole of issue #298: the module used to carry
    `("operator", "value_type", "value", "reason")` by hand, so a nullable string
    property added to QUERY_INTENT_SCHEMA was silently exempt from trimming,
    blank-to-null normalisation, and enum checking until somebody remembered to
    retype its name here.
    """
    synthetic = _synthetic_intent_schema()

    derived = _nullable_string_fields(synthetic)

    assert "unit" in derived
    assert "note" in derived


def test_nullable_string_fields_exclude_the_other_nullable_types():
    """Nullable is not enough -- the value has to be a string.

    `relation_candidates` is `["array", "null"]` and the target properties are
    `["object", "null"]`; a predicate written as `"null" in type` would pull all
    four in and hand them to `_clean_optional_string`, which trims strings. `kind`
    is a bare `"string"` and is not nullable at all.
    """
    derived = _nullable_string_fields(_synthetic_intent_schema())

    assert "relation_candidates" not in derived
    assert "subject" not in derived
    assert "relation" not in derived
    assert "object" not in derived
    assert "kind" not in derived


def test_comparison_domains_follow_a_schema_it_has_never_seen():
    """A new enum-constrained property is validated against its enum, not ignored."""
    domains = _comparison_domains(_synthetic_intent_schema())

    assert domains["unit"] == frozenset({"kg", "m"})


def test_blank_nullable_fields_split_a_new_property_by_its_enum():
    """The blank-is-null special case follows the enum, on properties and all.

    An enum-constrained property has no spelling of null other than null, so "" on
    it is an off-schema value for `_validate_schema_domains` to reject. A nullable
    string the schema leaves open keeps the #237 tolerance.
    """
    blank_nullable = _blank_nullable_fields(_synthetic_intent_schema())

    assert "unit" not in blank_nullable
    assert "note" in blank_nullable


def _query_intent_field_line(entry):
    name, declaration = entry if isinstance(entry, tuple) else (entry, "str | None = None")
    return f"    {name}: {declaration}\n"


def _build_query_intent_module(monkeypatch, schema, extra_fields=()):
    """Run a fresh, isolated copy of query_intent.py against `schema`.

    A separate module object rather than `importlib.reload`: a reload that raises
    part-way leaves the real module half-rebuilt, and even a successful one swaps
    the identity of `QueryIntent`/`IntentTarget` out from under every test that
    imported them, so dataclass equality starts failing elsewhere in the session.

    `extra_fields` adds fields to the QueryIntent dataclass before the copy
    executes. That is the one step a schema addition still requires by hand, so a
    test that the *rest* is automatic has to perform it -- otherwise the copy
    stops at the import guard and the parse path is never reached. Patching the
    source is what makes the resulting module a faithful "developer added the
    property and the field, and did nothing else". An entry is either a name (a
    nullable string field) or a `(name, declaration)` pair, so a property that is
    not a string can be given the annotation and default its author would.
    """
    monkeypatch.setattr(llm_schema, "QUERY_INTENT_SCHEMA", schema)
    source = pathlib.Path(query_intent.__file__).read_text(encoding="utf-8")
    if extra_fields:
        anchor = "    reason: str | None = None\n"
        # A drifted anchor would silently add nothing and leave every assertion
        # below testing the unmodified module.
        assert source.count(anchor) == 1, "QueryIntent's last field is no longer `reason`"
        source = source.replace(
            anchor,
            anchor + "".join(_query_intent_field_line(entry) for entry in extra_fields),
        )
    name = "query_intent_under_test"
    spec = importlib.util.spec_from_file_location(name, query_intent.__file__)
    module = importlib.util.module_from_spec(spec)
    # `dataclasses` resolves a field's annotations through
    # `sys.modules[cls.__module__]`, so the copy has to be registered while it
    # executes or building QueryIntent dies on an unrelated AttributeError.
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(source, query_intent.__file__, "exec"), module.__dict__)
    return module


def test_a_schema_property_the_parser_never_reads_is_refused_at_import(monkeypatch):
    """Widening the allow-list without wiring the parser must fail, not discard.

    Deriving the allow-list alone moved the failure rather than closing it: the
    new key stopped being refused as an unexpected field and started being
    accepted and thrown away, because the QueryIntent construction names its
    kwargs by hand. A blank or off-enum value then came back as None instead of
    being rejected -- quietly wrong rather than loudly refused, which is worse
    than the error it replaced.

    Asserting `"unit" in _intent_field_names(synthetic)` did not project any of
    that; it restated the derivation instead of exercising what the derivation was
    for, which is how the drop stayed green. This builds the module against a
    schema it has never seen and so fails both if the allow-list stops following
    the schema and if the check that the parser reads what it admits goes away.
    """
    with pytest.raises(RuntimeError) as excinfo:
        _build_query_intent_module(monkeypatch, _synthetic_intent_schema())

    message = str(excinfo.value)
    assert "unit" in message
    assert "note" in message
    # Not laundered into LLMError: no provider output can reach this, so calling
    # it a schema mismatch would blame the provider for a local wiring bug.
    # `parse_query_intent` converts KeyError/TypeError/ValueError; this is raised
    # at import, outside that path entirely.
    assert not isinstance(excinfo.value, LLMError)


def test_the_import_check_passes_on_the_schema_the_parser_does_read(monkeypatch):
    """The guard has to be satisfiable, or its red is worth nothing.

    Without this, a check hard-wired to raise would pass the test above while
    making the package unimportable.
    """
    module = _build_query_intent_module(monkeypatch, copy.deepcopy(QUERY_INTENT_SCHEMA))

    assert module.QUERY_INTENT_FIELDS == tuple(QUERY_INTENT_SCHEMA["properties"])


def test_a_new_enum_property_is_validated_on_parse_with_no_wiring(monkeypatch):
    """The acceptance of #298: a schema addition rejects blank without a code change.

    Deriving the allow-list and the name lists left the QueryIntent construction
    written out by hand, so `unit` was admitted as a key and then never read: the
    value was taken from the provider and dropped, and `unit: ""` or `unit: "lb"`
    came back as None instead of being refused. This follows the derived parser
    dispatch all the way to the value the caller gets back, which is the only place
    that difference shows -- `"unit" in _intent_field_names(synthetic)` restates
    the derivation and stayed green through the entire silent drop.

    The dataclass field is added because that is the one step still done by hand;
    nothing else about `unit` or `note` is named anywhere in the module.
    """
    module = _build_query_intent_module(
        monkeypatch, _synthetic_intent_schema(), extra_fields=("unit", "note")
    )

    # An on-enum value survives the round trip: the property is parsed, not
    # discarded, and the trimming every nullable string gets applies to it.
    accepted = module.parse_query_intent(_intent_payload(unit="kg", note="  fine  "))
    assert accepted.unit == "kg"
    assert accepted.note == "fine"

    # Blank on an enum property is an off-schema value, not a spelling of null:
    # "" is in no enum, so it must reach the domain check rather than be
    # normalised away. Off-enum is refused the same way.
    for rejected in ("", "   ", "lb"):
        with pytest.raises(LLMError) as excinfo:
            module.parse_query_intent(_intent_payload(unit=rejected))
        assert "unit must be one of kg, m" in str(excinfo.value)

    # The property the schema leaves open keeps the #237 blank-is-null tolerance,
    # so the new rule splits on the enum rather than on the property being new.
    assert module.parse_query_intent(_intent_payload(note="")).note is None


def test_a_property_type_with_no_parser_is_refused_rather_than_guessed(monkeypatch):
    """Dispatching on the declared type must not fall back to the string parser.

    A default would hand `["integer", "null"]` to `_parse_optional_string`, which
    rejects a non-string with "must be a string" -- reporting schema-legal provider
    output as a violation. Failing at import names the property and the table to
    extend instead.
    """
    schema = copy.deepcopy(QUERY_INTENT_SCHEMA)
    schema["properties"]["retries"] = {"type": ["integer", "null"]}
    schema["required"] = list(schema["properties"])

    with pytest.raises(RuntimeError) as excinfo:
        _build_query_intent_module(monkeypatch, schema, extra_fields=("retries",))

    assert "retries" in str(excinfo.value)
    assert not isinstance(excinfo.value, LLMError)


def _intent_schema_with(name, spec):
    """QUERY_INTENT_SCHEMA plus one property the module has never seen."""
    schema = copy.deepcopy(QUERY_INTENT_SCHEMA)
    schema["properties"][name] = spec
    schema["required"] = list(schema["properties"])
    return schema


_DROP = object()


def _target_like(**overrides):
    """QUERY_INTENT_TARGET_SCHEMA with parts replaced, or dropped via `_DROP`.

    Written as a diff against the real target schema so each case below says
    exactly which part of the shape it breaks -- and so a case cannot pass by
    breaking something else too.
    """
    spec = copy.deepcopy(QUERY_INTENT_TARGET_SCHEMA)
    for key, value in overrides.items():
        if value is _DROP:
            del spec[key]
        else:
            spec[key] = value
    return spec


def _target_properties(**overrides):
    properties = copy.deepcopy(QUERY_INTENT_TARGET_SCHEMA["properties"])
    properties.update(overrides)
    return properties


# Each entry declares an object among its types -- all but the last are the
# `["object", "null"]` that `subject`, `relation`, and `object` carry -- and each
# is legal JSON Schema whose values `_parse_intent_target` would refuse, one
# refusal per conjunct of the shape check. Dispatching on the top-level type
# alone assigned that parser to all of them, so the provider was handed a schema,
# obeyed it, and had its output reported as "did not match schema".
_UNSUPPORTED_NULLABLE_OBJECT_SHAPES = {
    # The maintainer's example on #352: a wholly different object.
    "a nested object with its own properties": {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["name"],
        "properties": {"name": {"type": "string", "minLength": 1}},
    },
    # `IntentTarget.__post_init__` refuses a kind outside INTENT_TARGET_KINDS.
    "a kind enum reaching outside the target kinds": _target_like(
        properties=_target_properties(
            kind={"type": "string", "enum": ["entity", "literal"]}
        )
    ),
    # No enum at all: every string is on-schema, and all but four are refused.
    "an unconstrained kind": _target_like(
        properties=_target_properties(kind={"type": "string"})
    ),
    # An enum admitting nothing: no output at all satisfies it.
    "an empty kind enum": _target_like(
        properties=_target_properties(kind={"type": "string", "enum": []})
    ),
    # A null kind is on-schema; `_parse_intent_target` demands a string.
    "a nullable kind": _target_like(
        properties=_target_properties(
            kind={"type": ["string", "null"], "enum": ["entity", None]}
        )
    ),
    # `_parse_intent_target` refuses any key beyond kind/value -- and `note` here
    # is optional, so the extra property is the only thing wrong with it.
    "an optional third property": _target_like(
        properties=_target_properties(note={"type": "string", "minLength": 1}),
    ),
    # Without `additionalProperties: False` the provider may add keys legally.
    "keys the schema does not close off": _target_like(additionalProperties=_DROP),
    # Without `required`, a missing `value` is on-schema and raises KeyError.
    "optional halves": _target_like(required=_DROP),
    # Without `minLength`, `value: ""` is on-schema and IntentTarget refuses it.
    "a value that may be blank": _target_like(
        properties=_target_properties(value={"type": "string"})
    ),
    # The bound present but set to the length that admits everything: `minLength:
    # 0` is exactly `""` again, said the long way. Dropping the key and writing
    # `0` are the same schema, so the check has to read the number rather than
    # settle for one being there.
    "a value bounded at zero length": _target_like(
        properties=_target_properties(value={"type": "string", "minLength": 0})
    ),
    # A nullable `value`: `{"kind": "entity", "value": null}` is on-schema, and
    # `_parse_intent_target` calls a null half "must be a string". The bound says
    # nothing about it -- `minLength` constrains the string branch alone -- so the
    # declared type has to be the string on its own, not a union containing one.
    "a value that may be null": _target_like(
        properties=_target_properties(value={"type": ["string", "null"], "minLength": 1})
    ),
    # Admitting a string alongside the object: `subject: "Acme"` is then on-schema
    # and `_parse_intent_target` refuses it as "must be an object". The extra
    # branch of the union is invisible to a check asking only whether `object` is
    # among the declared types.
    "a three-way union of object, null and string": _target_like(
        type=["object", "null", "string"]
    ),
}


@pytest.mark.parametrize(
    "spec",
    list(_UNSUPPORTED_NULLABLE_OBJECT_SHAPES.values()),
    ids=list(_UNSUPPORTED_NULLABLE_OBJECT_SHAPES),
)
def test_an_unsupported_nullable_object_property_is_refused_at_import(monkeypatch, spec):
    """A property admitting an object that is not the target shape fails at import.

    Sharing a top-level type with `subject` is not sharing a parser.
    `_parse_intent_target` reads one object -- `{kind, value}`, kind on
    INTENT_TARGET_KINDS, value non-blank -- and rejects everything else per parse,
    where `parse_query_intent` relabels the rejection as an `LLMError` saying the
    provider's output did not match the schema. For every property here that
    sentence is false: the output matches the schema, and the parser is the thing
    out of step. The whole point of #298's design is to refuse a shape it cannot
    parse instead of guessing at one, so the refusal belongs at import, before any
    provider is blamed.
    """
    with pytest.raises(RuntimeError) as excinfo:
        _build_query_intent_module(
            monkeypatch,
            _intent_schema_with("topic", spec),
            extra_fields=(("topic", "IntentTarget | None = None"),),
        )

    message = str(excinfo.value)
    assert "topic" in message
    # Not the "QueryIntent has no field for it" guard: the field was added, so
    # only the shape check can be speaking here.
    assert "matches no shape" in message
    assert not isinstance(excinfo.value, LLMError)


_UNSUPPORTED_NULLABLE_ARRAY_SHAPES = {
    # The maintainer's example on #352: schema-legal, and not a string array.
    # An array of the very objects `_parse_intent_target` reads, at that -- the
    # parser next door is no help, because the array parser is the one assigned.
    "an array of objects": {
        "type": ["array", "null"],
        "items": copy.deepcopy(QUERY_INTENT_TARGET_SCHEMA),
    },
    "an array of numbers": {"type": ["array", "null"], "items": {"type": "number"}},
    # Unconstrained items admit objects, numbers and nulls alike.
    "an array with no item schema": {"type": ["array", "null"]},
    # `_clean_required_string` refuses a blank candidate, so without the bound
    # the schema admits an item the parse path calls a provider violation.
    "an array whose items may be blank": {
        "type": ["array", "null"],
        "items": {"type": "string"},
    },
    # A tuple schema: the leading entries are not the `items` string at all.
    "a tuple whose leading entry is an object": {
        "type": ["array", "null"],
        "prefixItems": [copy.deepcopy(QUERY_INTENT_TARGET_SCHEMA)],
        "items": {"type": "string", "minLength": 1},
    },
    # `minLength: 0` admits `[""]` exactly as omitting the key does, so a check
    # content with the bound merely being present would let the blank item back
    # in under a number.
    "an array whose items are bounded at zero length": {
        "type": ["array", "null"],
        "items": {"type": "string", "minLength": 0},
    },
    # `["alpha", null]` is on-schema here and `_parse_relation_candidates` calls
    # it "items must be strings". `minLength` does not exclude the null branch --
    # it only bounds the string one -- so the item type has to be the bare string.
    "an array whose items may be null": {
        "type": ["array", "null"],
        "items": {"type": ["string", "null"], "minLength": 1},
    },
    # A bare string is on-schema for this property, and the parser refuses
    # anything that is not a list. The array branch being present says nothing
    # about the branches beside it.
    "a three-way union of array, null and string": {
        "type": ["array", "null", "string"],
        "items": {"type": "string", "minLength": 1},
    },
}


@pytest.mark.parametrize(
    "spec",
    list(_UNSUPPORTED_NULLABLE_ARRAY_SHAPES.values()),
    ids=list(_UNSUPPORTED_NULLABLE_ARRAY_SHAPES),
)
def test_an_unsupported_nullable_array_property_is_refused_at_import(monkeypatch, spec):
    """A property admitting an array that is not a non-blank string array fails too.

    `_parse_relation_candidates` is the only array parser, and it reads an array
    of non-blank strings; every other element type it sees becomes an `LLMError`
    blaming the provider for output its own schema allowed. Same failure as the
    object case, same fix: refuse the shape at import rather than assign a parser
    to it because the top-level type matched.
    """
    with pytest.raises(RuntimeError) as excinfo:
        _build_query_intent_module(
            monkeypatch,
            _intent_schema_with("aliases", spec),
            extra_fields=(("aliases", "tuple[str, ...] = ()"),),
        )

    message = str(excinfo.value)
    assert "aliases" in message
    assert "matches no shape" in message
    assert not isinstance(excinfo.value, LLMError)


def test_a_property_admitting_a_string_among_other_types_is_refused_at_import(monkeypatch):
    """The string half of the same discrimination: a union is not a nullable string.

    `_parse_optional_string` accepts every string and null, and refuses anything
    else as "must be a string or null" -- so it reads `["string", "null"]` and
    only that. On the union here `"count": 3` is output the schema explicitly
    invites, and a check asking merely whether `string` is among the declared
    types would hand the property to that parser and have it call the provider's
    obedient `3` a schema violation.

    This is the case `["integer", "null"]` above cannot make: there the property
    admits no string at all, so every candidate check rejects it. It takes a type
    list containing `string` *and* something else to tell "is exactly a nullable
    string" apart from "mentions string somewhere".
    """
    with pytest.raises(RuntimeError) as excinfo:
        _build_query_intent_module(
            monkeypatch,
            _intent_schema_with("count", {"type": ["string", "null", "integer"]}),
            extra_fields=(("count", "str | int | None = None"),),
        )

    message = str(excinfo.value)
    assert "count" in message
    # The shape check speaking, not the missing-field guard: the field was added.
    assert "matches no shape" in message
    assert not isinstance(excinfo.value, LLMError)


def test_a_second_target_shaped_property_is_parsed_with_no_wiring(monkeypatch):
    """The shape check must admit the shape, not just the three known names.

    Rejecting every object property outside `subject`/`relation`/`object` would
    pass the refusal tests above while throwing away the derivation #298 is for:
    a property that *is* the target shape has to keep being parsed with no wiring
    step. So this follows a schema-only addition to the value the caller gets
    back -- a parsed `IntentTarget` with its value trimmed, not the raw dict and
    not None -- and to the rejection of an off-enum kind, which is what says the
    property is being validated rather than passed through.
    """
    module = _build_query_intent_module(
        monkeypatch,
        _intent_schema_with("topic", copy.deepcopy(QUERY_INTENT_TARGET_SCHEMA)),
        extra_fields=(("topic", "IntentTarget | None = None"),),
    )

    parsed = module.parse_query_intent(
        _intent_payload(topic={"kind": "entity", "value": "  Acme  "})
    )
    assert parsed.topic == module.IntentTarget("entity", "Acme")

    # Null and an absent key both mean absent, as for every nullable property.
    assert module.parse_query_intent(_intent_payload(topic=None)).topic is None
    assert module.parse_query_intent(_intent_payload()).topic is None

    # Off-enum: refused here, and refused as a provider error, because this one
    # really is output the schema forbids.
    with pytest.raises(LLMError):
        module.parse_query_intent(_intent_payload(topic={"kind": "literal", "value": "x"}))


def test_a_second_string_array_property_is_parsed_with_no_wiring(monkeypatch):
    """The array half of the same discrimination: shape admitted, name not needed."""
    module = _build_query_intent_module(
        monkeypatch,
        _intent_schema_with(
            "aliases", {"type": ["array", "null"], "items": {"type": "string", "minLength": 1}}
        ),
        extra_fields=(("aliases", "tuple[str, ...] = ()"),),
    )

    parsed = module.parse_query_intent(_intent_payload(aliases=["alpha", "beta"]))
    assert parsed.aliases == ("alpha", "beta")
    assert module.parse_query_intent(_intent_payload(aliases=None)).aliases == ()

    # Parsed rather than stored: a non-string item is refused, which a property
    # nobody reads could not do.
    with pytest.raises(LLMError):
        module.parse_query_intent(_intent_payload(aliases=[{"kind": "entity", "value": "x"}]))


def test_the_target_kinds_the_shape_check_admits_are_the_ones_intent_target_takes():
    """One set, not two: the check and `__post_init__` must not drift apart.

    A schema `kind` enum is safe exactly when every value on it constructs an
    `IntentTarget`. Were the check to keep its own copy of the kinds, widening
    one would let a schema through whose legal output the other refuses -- the
    very mismatch this shape check exists to catch, reintroduced inside it.
    """
    for kind in query_intent.INTENT_TARGET_KINDS:
        assert IntentTarget(kind, "x").kind == kind

    with pytest.raises(ValueError):
        IntentTarget("literal", "x")
    assert "literal" not in query_intent.INTENT_TARGET_KINDS


def test_derived_nullable_string_fields_all_exist_on_the_query_intent_dataclass():
    """`__post_init__` getattr's every derived name, so a stray one is AttributeError.

    Deliberately a red test rather than a `hasattr` skip in the loop: skipping the
    names the dataclass lacks would let a nullable string property added to
    QUERY_INTENT_SCHEMA through parsing unnormalised and unvalidated, which is the
    exact hole #298 closes -- it would just move from "the hand list forgot it" to
    "the loop stepped over it". Adding a property to the schema is meant to force
    adding the field here; failing loudly is how that gets said.
    """
    dataclass_field_names = {f.name for f in dataclasses.fields(QueryIntent)}

    assert set(_nullable_string_fields(QUERY_INTENT_SCHEMA)) <= dataclass_field_names


@pytest.mark.parametrize("blank", ["", "   "])
def test_parse_query_intent_still_rejects_unknown_with_a_blank_reason(blank):
    """Normalizing blank to null must not let a reasonless unknown through."""
    with pytest.raises(LLMError):
        parse_query_intent(_intent_payload(kind="unknown_or_unsupported", reason=blank))


def test_parse_query_intent_still_rejects_compare_with_a_blanked_out_field():
    """A blank comparison field is an absent one, and compare_typed_value needs it."""
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="compare_typed_value",
                subject={"kind": "entity", "value": "샘플항목"},
                relation={"kind": "relation", "value": "수량"},
                operator="",
                value_type="number",
                value="10",
            )
        )


def test_parse_query_intent_treats_a_missing_nullable_key_as_null():
    """Prompt-only providers render the schema as text and drop null keys.

    `claude_cli` only renders the schema into the prompt, so an omitted key is
    routine; the schema already declares null legal for every nullable field, so
    "absent" and "null" have to mean the same thing. `kind` stays required.
    """
    intent = parse_query_intent(
        {
            "kind": "lookup_object",
            "subject": {"kind": "entity", "value": "샘플인물"},
            "relation": {"kind": "relation", "value": "역할"},
        }
    )

    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.subject == IntentTarget("entity", "샘플인물")
    assert intent.reason is None


@pytest.mark.parametrize(
    ("kind", "fields"),
    [entry for entry in _REASON_TOLERANT_INTENTS if entry[0] != "compare_typed_value"],
    ids=[kind for kind, _ in _REASON_TOLERANT_INTENTS if kind != "compare_typed_value"],
)
def test_parse_query_intent_accepts_advisory_comparison_fields_on_every_kind(kind, fields):
    """A stray comparison field is advisory too, for the same reasons `reason` is.

    `operator` is a required, enum-constrained property of QUERY_INTENT_SCHEMA
    whose enum admits `"="`, so a model answering "Who is the CEO of Acme?" with
    `lookup_object` + `operator: "="` is emitting perfectly schema-legal output.
    Nothing outside this module reads operator/value_type/value, and the planner
    has no `compare_typed_value` branch at all, so rejecting them threw away a
    correctly classified intent over fields no one consumes -- the same failure
    as issue #237, one field over.
    """
    intent = parse_query_intent(
        _intent_payload(
            kind=kind, operator="=", value_type="date", value="CEO", **fields
        )
    )

    assert intent.kind == QueryIntentKind(kind)
    assert intent.operator == "="


@pytest.mark.parametrize(
    ("kind", "fields"),
    [entry for entry in _REASON_TOLERANT_INTENTS if entry[0] != "compare_typed_value"],
    ids=[kind for kind, _ in _REASON_TOLERANT_INTENTS if kind != "compare_typed_value"],
)
@pytest.mark.parametrize(
    "invalid", [{"operator": "contains"}, {"value_type": "duration"}]
)
def test_parse_query_intent_rejects_off_schema_comparison_fields_on_every_kind(
    kind, fields, invalid
):
    """Advisory does not mean unvalidated: a non-null value must still be on-schema.

    Ignoring a *schema-legal* stray `operator: "="` is the #237 fix. Ignoring
    `operator: "contains"` would be something else -- QUERY_INTENT_SCHEMA's enum
    forbids it, so the validator would be accepting output the contract every
    adapter is handed calls invalid, and a strict-mode provider could never send
    it in the first place. The validator boundary must not sit wider than the
    schema's.
    """
    payload = _intent_payload(kind=kind, **fields)
    payload.update(invalid)

    with pytest.raises(LLMError):
        parse_query_intent(payload)


def test_query_intent_comparison_domains_are_taken_from_the_schema():
    """The accepted values are read off QUERY_INTENT_SCHEMA, not restated.

    Two hand-maintained copies of an enum drift, and the drift is invisible until
    a provider is rejected for on-schema output (or accepted for off-schema
    output). Widening the schema enum must widen the validator with it.
    """
    assert QUERY_INTENT_COMPARISON_DOMAINS["operator"] == frozenset(
        value for value in QUERY_INTENT_SCHEMA["properties"]["operator"]["enum"] if value is not None
    )
    assert QUERY_INTENT_COMPARISON_DOMAINS["value_type"] == frozenset(
        value
        for value in QUERY_INTENT_SCHEMA["properties"]["value_type"]["enum"]
        if value is not None
    )


def test_parse_query_intent_still_requires_comparison_fields_for_compare():
    """Relaxing other kinds must not make compare_typed_value's own fields optional."""
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="compare_typed_value",
                subject={"kind": "entity", "value": "샘플항목"},
                relation={"kind": "relation", "value": "수량"},
                value_type="number",
                value="10",
            )
        )


def test_parse_query_intent_still_rejects_unknown_carrying_comparison_fields():
    """`unknown_or_unsupported` still accepts nothing but kind and reason."""
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="unknown_or_unsupported",
                operator="=",
                reason="requires planning",
            )
        )


def test_parse_query_intent_still_requires_a_reason_for_unknown():
    with pytest.raises(LLMError):
        parse_query_intent(_intent_payload(kind="unknown_or_unsupported", reason=None))


def test_parse_query_intent_still_rejects_unknown_carrying_query_fields():
    with pytest.raises(LLMError):
        parse_query_intent(
            _intent_payload(
                kind="unknown_or_unsupported",
                subject={"kind": "entity", "value": "샘플인물"},
                reason="requires planning",
            )
        )


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
            kind="discover_entity_relations",
            subject=None,
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "relation", "value": "provides"},
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            object={"kind": "entity", "value": "Sample Object"},
            reason=None,
        ),
        _intent_payload(
            kind="discover_entity_relations",
            subject={"kind": "entity", "value": "Sample Entity"},
            relation={"kind": "relation", "value": "provides"},
            relation_candidates=["offers"],
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
    assert "Subject is the entity, row key, or record owner" in EXTRACTION_SYSTEM
    assert "Relation is a concise predicate" in EXTRACTION_SYSTEM
    assert "Object is the related entity or value" in EXTRACTION_SYSTEM
    assert "instead of copying whole source phrases" in EXTRACTION_SYSTEM
    assert "named-entity spelling for subjects and objects" in EXTRACTION_SYSTEM
    assert "prefer concise English canonical predicates" in EXTRACTION_SYSTEM
    assert "such as `role`, `affiliation`, `provides`, or `value`" in EXTRACTION_SYSTEM
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
    assert "Extraction is not a summary" in EXTRACTION_SYSTEM
    assert "Traverse every visible section, table, list" in EXTRACTION_SYSTEM
    assert "Do not sample representative items" in EXTRACTION_SYSTEM
    assert "many small source-backed triples" in EXTRACTION_SYSTEM
    assert "For each sentence, table row, bullet, list item, or layout record" in EXTRACTION_SYSTEM
    assert "Self-check before returning" in EXTRACTION_SYSTEM


def test_extraction_prompt_uses_factlog_table_mapping_rules():
    assert "Extract tables and structured records row by row" in EXTRACTION_SYSTEM
    assert "Use the row-identifying key" in EXTRACTION_SYSTEM
    assert "as subject" in EXTRACTION_SYSTEM
    assert "Use the column header or item label as relation" in EXTRACTION_SYSTEM
    assert "Use the cell value as object" in EXTRACTION_SYSTEM
    assert "emit separate facts" in EXTRACTION_SYSTEM
    assert "Do not include CSV headers" in EXTRACTION_SYSTEM


def test_extraction_prompt_prefers_typed_literal_terms():
    assert "`date(YYYY)`" in EXTRACTION_SYSTEM
    assert "`ordinal(N)`" in EXTRACTION_SYSTEM
    assert "`amount(N,\"unit\")`" in EXTRACTION_SYSTEM
    assert "`number(N)`" in EXTRACTION_SYSTEM
    assert "Typed literals are object values, never subjects or relations" in EXTRACTION_SYSTEM
    assert "or integer" in EXTRACTION_SYSTEM
    assert "number(-2.5)" not in EXTRACTION_SYSTEM
    assert 'Emit these typed literal objects with object.kind="term"' in EXTRACTION_SYSTEM
    assert 'do not emit compound-looking text as object.kind="string"' in EXTRACTION_SYSTEM
    assert "relation `number(8)` and object `명`" in EXTRACTION_SYSTEM
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


def test_parse_facts_rejects_invalid_explicit_term_slots():
    with pytest.raises(LLMError, match="malformed fact object"):
        parse_facts(
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


def test_parse_facts_rejects_variable_bearing_explicit_term_slots():
    with pytest.raises(LLMError, match="structural term must be ground"):
        parse_facts(
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
        )


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


def test_parse_facts_accepts_batch_of_only_valid_facts():
    """A batch with no schema violations is parsed in full -- nothing raises."""
    facts = parse_facts(
        {
            "facts": [
                {
                    "subject": "Ada",
                    "relation": "wrote",
                    "object": "notes",
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
        ("Ada", "wrote", "notes"),
        ("Ada", "is_a", "mathematician"),
    ]


def test_parse_facts_raises_when_valid_and_malformed_are_mixed():
    """A violation next to valid facts must fail the whole batch, not drop.

    Dropping the off-schema item while returning the valid one would report the
    chunk as a success with the violating fact silently gone. `base.py` promises
    `LLMError` on any schema violation so the caller can retry deterministically
    (issue #168), so the mixed batch raises rather than smuggling a partial
    result past the retry path.
    """
    with pytest.raises(LLMError):
        parse_facts(
            {
                "facts": [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": "Ada",
                        "relation": "",  # empty slot is a schema violation
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            }
        )


def test_parse_facts_raises_when_every_item_is_malformed():
    """A batch of only violations also fails loudly."""
    with pytest.raises(LLMError):
        parse_facts(
            {
                "facts": [
                    {
                        "subject": "",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": "Ada",
                        "relation": "",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            }
        )


def _widget_fact(**overrides):
    fact = {
        "subject": "Widget",
        "relation": "made_by",
        "object": "Gadget",
        "confidence": 0.9,
        "note": "",
    }
    fact.update(overrides)
    return fact


@pytest.mark.parametrize(
    "overrides",
    [
        # The repros as filed: an off-schema JSON type in a bare slot.
        {"subject": ["Widget", "Alpha"]},
        {"object": {"city": "Gadgetville"}},
        {"subject": None},
        {"subject": "   "},
        # The same off-schema types smuggled through the {kind,value} branch,
        # which str() used to coerce into plausible-looking review-queue text.
        {"subject": {"kind": "string", "value": ["Widget", "Alpha"]}},
        {"subject": {"kind": "string", "value": None}},
        {"subject": {"kind": "string", "value": "   "}},
        {"object": {"kind": "string", "value": {"city": "Gadgetville"}}},
        {"object": {"kind": "term", "value": {"functor": "maker"}}},
        {"relation": {"kind": ["string"], "value": "made_by"}},
        # `note` is declared {"type": "string"}.
        {"note": ["off", "schema"]},
        # `confidence` is declared {"type": "number", "minimum": 0, "maximum": 1}.
        {"confidence": True},
        {"confidence": 90},
        {"confidence": -1},
    ],
)
def test_parse_facts_rejects_off_schema_slot_types_instead_of_coercing(overrides):
    """The declared schema types every slot as a string; str() coercion would let
    lists/objects/null reach the review queue as text like "['Widget', 'Alpha']"."""
    with pytest.raises(LLMError):
        parse_facts({"facts": [_widget_fact(**overrides)]})


def test_parse_facts_still_accepts_plain_string_slots():
    fact = parse_facts({"facts": [_widget_fact()]})[0]

    assert (fact.subject, fact.relation, fact.object) == ("Widget", "made_by", "Gadget")
    assert fact.subject_kind == fact.relation_kind == fact.object_kind == "string"
    assert fact.confidence == 0.9


def test_parse_facts_still_accepts_explicit_ground_term_slots():
    """Fact-storage boundary: a structural fact is only a term when the model says
    so with {"kind": "term"}. Enforcing slot types must not break that path."""
    fact = parse_facts(
        {"facts": [_widget_fact(object={"kind": "term", "value": "maker(gadget)"})]}
    )[0]

    assert fact.object == "maker(gadget)"
    assert fact.object_kind == "term"
    assert fact.note == ""


def test_parse_facts_keeps_plain_functor_text_as_a_string_literal():
    """A bare "maker(gadget)" string is a StringLit, never reinterpreted as a compound."""
    fact = parse_facts({"facts": [_widget_fact(object="maker(gadget)")]})[0]

    assert fact.object == "maker(gadget)"
    assert fact.object_kind == "string"


@pytest.mark.parametrize("note", [None, "seen in the spec"])
def test_parse_facts_accepts_missing_or_null_note(note):
    payload = _widget_fact()
    if note is None:
        payload.pop("note")
    else:
        payload["note"] = note

    assert parse_facts({"facts": [payload]})[0].note == (note or "")


def test_parse_facts_tolerates_numeric_string_confidence_from_prompt_only_providers():
    assert parse_facts({"facts": [_widget_fact(confidence="0.75")]})[0].confidence == 0.75
