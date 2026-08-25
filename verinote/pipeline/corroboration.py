# SPDX-License-Identifier: MPL-2.0
"""Source-support and single-valued conflict views for engine-input facts.

Borrowed from factlog's deterministic trust signals: distinct source support is
reported separately from LLM confidence, and single-valued conflicts are judged
only over facts that have crossed the review gate.
"""

from __future__ import annotations

import datetime
import decimal
from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata
from typing import Any, Iterable, Mapping

from verinote.policy_defaults import (
    DEFAULT_RELATION_ALIASES,
    RELATION_ALIASES_RELPATH,
    TYPED_RELATIONS_RELPATH,
)
from verinote.store import Store, is_engine_input

_FUNCTIONAL_RE = re.compile(r'functional\("((?:\\.|[^"\\])*)"\)\.')
_TYPED_REL_RE = re.compile(
    r"^(?:`(?P<qname>[^`]+)`|(?P<name>\S+))\s*:\s*(?P<type>\w+)\s+as\s+(?P<alias>\S+)"
    r"(?:\s*\((?P<units>[^)]*)\))?\s*$"
)
_DATE_RE = re.compile(r"^(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?$")
_DATE_COMPOUND_RE = re.compile(
    r"^date\(\s*(\d{4})\s*,\s*(\d{1,2})(?:\s*,\s*(\d{1,2}))?\s*\)$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_NUMBER_COMPOUND_RE = re.compile(
    r"^number\(\s*\"?(-?\d[\d,]*(?:\.\d+)?)\"?\s*\)$",
    re.IGNORECASE,
)
_ORDINAL_KO_RE = re.compile(r"^제?(\d+)\s*(?:호|위|번|차|등|째)$")
_ORDINAL_EN_RE = re.compile(r"^(\d+)\s*(?:st|nd|rd|th)$", re.IGNORECASE)
_ORDINAL_COMPOUND_RE = re.compile(r"^ordinal\(\s*(\d+)\s*\)$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"^(?P<num>-?\d[\d,]*(?:\.\d+)?) ?(?P<unit>\D+)$")
_AMOUNT_COMPOUND_RE = re.compile(
    r'^amount\(\s*"?(?P<num>-?\d[\d,]*(?:\.\d+)?)"?\s*,\s*'
    r'(?:"(?P<qunit>[^"]*)"|(?P<unit>[^,)"]+))\s*\)$',
    re.IGNORECASE,
)
_NUMBER_SCALE = 1000
_CURRENCY_MARKER = "원"
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_TYPED_TYPES = frozenset({"date", "number", "ordinal", "amount"})
_DEFAULT_AMOUNT_UNITS = {
    "원": 1,
    "천": 10**3,
    "만": 10**4,
    "억": 10**8,
    "조": 10**12,
}

class CorroborationPolicyError(ValueError):
    """Raised when optional corroboration policy files are malformed."""


def policy_file_failure(read, relpath: str) -> str | None:
    """Normalise ONE trust-policy file read into a message, or None.

    `read` is a zero-argument callable so the try wraps EXACTLY ONE CALL, and
    that call touches no database: `store_relation_aliases` and
    `store_typed_relations` each read `store.db_path` as an attribute, stat
    their file, and parse it. So the only thing this can swallow is a failure to
    read or parse that one file.

    THE ONLY COPY, and it lives here because this is where its parts live:
    `CorroborationPolicyError` is defined in this module and the relpath arrives
    as a parameter, so it needs nothing imported to sit here. It was in
    `query_schema.py` until #590 gave it callers in four modules, at which point
    a function about policy files reached from `verify.py` and
    `report_trace.py` was importing from a module about query schemas.

    #585 wrote it nested inside `create_app`, and #591 added a second copy on
    the stated ground that the first was "closed over the request context" and
    so could not be imported. That was false -- measured with the symbol table,
    it closed over nothing; nesting was where it had been typed, not a
    constraint. The web copy is gone and `web/app.py::_trust_policy_failure`
    delegates to `query_schema_policy_failure`, which calls this. Do not
    reintroduce a second copy: the argument for doing so has already been wrong
    once.
    """
    try:
        read()
    except CorroborationPolicyError as exc:
        # G1. Already normalised, and its message already begins with the file's
        # own name (`relation-aliases.md:1: expected …`, `typed-relations.md:
        # alias 'x' used for both …`). Prefixing it below would say the file
        # twice AND would misstate what happened -- the file WAS read; it parsed
        # and failed. Must stay ABOVE G2.
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - normalise every policy-read failure
        # G2. BROAD, NOT A TYPE LIST. `UnicodeDecodeError` (a file saved as
        # cp949) descends from `ValueError` as `CorroborationPolicyError` does
        # but is no subclass of it; `PermissionError` is not a `ValueError` at
        # all. NAME THE FILE: `str(UnicodeDecodeError)` is a byte offset and no
        # path.
        return f"{relpath} could not be read: {exc}"
    return None


@dataclass(frozen=True)
class FactSupport:
    subject: str
    relation: str
    object: str
    sources: tuple[str, ...]

    @property
    def source_count(self) -> int:
        return len(self.sources)


@dataclass(frozen=True)
class CompetingValue:
    object: str
    sources: tuple[str, ...]

    @property
    def source_count(self) -> int:
        return len(self.sources)


@dataclass(frozen=True)
class SingleValuedConflict:
    subject: str
    relation: str
    values: tuple[CompetingValue, ...]


@dataclass(frozen=True)
class TypedRelationSpec:
    type: str
    alias: str
    units: dict[str, int] | None = None


def functional_relations(policy_dl: str) -> set[str]:
    """Parse ``functional("rel").`` declarations from a policy program."""
    if not isinstance(policy_dl, str):
        raise TypeError("policy_dl must be a str")
    return {_unescape(m.group(1)) for m in _FUNCTIONAL_RE.finditer(policy_dl)}


def store_functional_relations(store: Store) -> set[str]:
    """Return the relation names treated as single-valued for this KB."""
    from verinote.pipeline.verify import load_policy

    return functional_relations(load_policy(store))


def relation_aliases(text: str) -> dict[str, str]:
    """Parse factlog-style relation aliases into ``{raw: canonical}``."""
    aliases: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw_text, separator, canonical_text = stripped.partition("->")
        if not separator or "->" in canonical_text:
            raise CorroborationPolicyError(
                f"relation-aliases.md:{line_no}: expected `raw` -> `canonical`"
            )
        raw = _relation_alias_token(raw_text, line_no=line_no)
        canonical = _relation_alias_token(canonical_text, line_no=line_no)
        if raw == canonical:
            raise CorroborationPolicyError(
                f"relation-aliases.md: self-map {raw!r} is not allowed"
            )
        if raw in aliases and aliases[raw] != canonical:
            raise CorroborationPolicyError(
                f"relation-aliases.md: {raw!r} mapped to both "
                f"{aliases[raw]!r} and {canonical!r}"
            )
        aliases[raw] = canonical
    canonical_values = set(aliases.values())
    for raw in aliases:
        if raw in canonical_values:
            raise CorroborationPolicyError(
                f"relation-aliases.md: {raw!r} is both raw and canonical"
            )
    return aliases


def _relation_alias_token(text: str, *, line_no: int) -> str:
    token = text.strip()
    if not token:
        raise CorroborationPolicyError(
            f"relation-aliases.md:{line_no}: alias names must not be empty"
        )
    if token.startswith("`") or token.endswith("`"):
        if len(token) < 2 or not token.startswith("`") or not token.endswith("`"):
            raise CorroborationPolicyError(
                f"relation-aliases.md:{line_no}: malformed backtick alias"
            )
        token = token[1:-1].strip()
        if "`" in token:
            raise CorroborationPolicyError(
                f"relation-aliases.md:{line_no}: malformed backtick alias"
            )
    elif "`" in token:
        raise CorroborationPolicyError(
            f"relation-aliases.md:{line_no}: malformed backtick alias"
        )
    token = unicodedata.normalize("NFC", token)
    if not token:
        raise CorroborationPolicyError(
            f"relation-aliases.md:{line_no}: alias names must not be empty"
        )
    return token


def store_relation_aliases(store: Store) -> dict[str, str]:
    path = store.db_path.parent / RELATION_ALIASES_RELPATH
    if not path.is_file():
        return relation_aliases(DEFAULT_RELATION_ALIASES)
    user_aliases = relation_aliases(path.read_text(encoding="utf-8"))
    return merge_default_relation_aliases(user_aliases)


def merge_default_relation_aliases(user_aliases: dict[str, str]) -> dict[str, str]:
    defaults = relation_aliases(DEFAULT_RELATION_ALIASES)
    user_raw = {unicodedata.normalize("NFC", raw) for raw in user_aliases}
    user_canonical = {
        unicodedata.normalize("NFC", canonical)
        for canonical in user_aliases.values()
    }
    merged = {
        raw: canonical
        for raw, canonical in defaults.items()
        if unicodedata.normalize("NFC", raw) not in user_canonical
        and unicodedata.normalize("NFC", canonical) not in user_raw
    }
    merged.update(user_aliases)
    return merged


def typed_relations(text: str) -> dict[str, TypedRelationSpec]:
    """Parse factlog-style ``policy/typed-relations.md`` declarations.

    A line that is neither blank nor a ``#`` comment MUST parse (#589). This is
    `relation_aliases`'s rule, adopted rather than invented -- the two files are
    siblings and a user has no way to know why one would forgive a typo the
    other refuses. Markdown headings survive because they begin with ``#``.

    Before this, anything the shape did not match was skipped and the function
    returned whatever else it found. One mistyped line silently voided one
    declaration with no error, no warning, and -- because `{}` is also the
    normal state of a KB with no typed file -- nothing distinguishable in any
    rendered value. That is why the report has to happen HERE, at the parse, and
    cannot be inferred downstream.

    Raising is safe now and was not before #585: `store_typed_relations`'s
    callers degrade on a `CorroborationPolicyError` instead of returning 500.
    """
    specs: dict[str, TypedRelationSpec] = {}
    aliases: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"\s*#.*$", "", stripped).strip()
        match = _TYPED_REL_RE.match(stripped)
        if match is None:
            raise CorroborationPolicyError(
                f"typed-relations.md:{line_no}: expected "
                f"`- name : type as alias`, got {stripped!r}"
            )
        name = unicodedata.normalize(
            "NFC", (match.group("qname") or match.group("name")).strip()
        )
        type_tag = match.group("type").strip()
        alias = match.group("alias").strip()
        if type_tag not in _TYPED_TYPES:
            # Matched the declaration shape, so it is unambiguously an intended
            # declaration -- reporting it cannot be mistaken for prose.
            raise CorroborationPolicyError(
                f"typed-relations.md:{line_no}: unknown type {type_tag!r} for "
                f"{name!r}; known types are {', '.join(sorted(_TYPED_TYPES))}"
            )
        if alias in aliases and aliases[alias] != name:
            raise CorroborationPolicyError(
                f"typed-relations.md: alias {alias!r} used for both "
                f"{aliases[alias]!r} and {name!r}"
            )
        aliases[alias] = name
        units = None
        if match.group("units") is not None:
            if type_tag != "amount":
                raise CorroborationPolicyError(
                    f"typed-relations.md: units are only valid for amount: {name!r}"
                )
            units = _parse_amount_units(match.group("units"))
        specs[name] = TypedRelationSpec(type=type_tag, alias=alias, units=units)
    return specs


def store_typed_relations(store: Store) -> dict[str, TypedRelationSpec]:
    path = store.db_path.parent / TYPED_RELATIONS_RELPATH
    if not path.is_file():
        return {}
    specs = typed_relations(path.read_text(encoding="utf-8"))
    try:
        aliases = store_relation_aliases(store)
    except Exception:  # noqa: BLE001 - ANY alias-read failure, not one class
        # The alias file has its own guard and its own message. A typed-file
        # reader that reported the ALIAS file's failure would name the wrong
        # file, so the collision check is skipped and the alias guard speaks.
        #
        # `except Exception` and not `except CorroborationPolicyError`, matching
        # `policy_file_failure`'s G2 clause above: a cp949 alias file raises
        # `UnicodeDecodeError`, a SIBLING of `ValueError` and not a subclass of
        # the policy error, so the narrow form let it through and the typed
        # guard wrapped it as "policy/typed-relations.md could not be read" --
        # naming the wrong file, with a byte offset from the alias file.
        # Nothing is lost by catching broadly, and the reason is a PROPERTY
        # rather than a count: EVERY caller of this function reads the alias
        # file itself, so a swallowed alias failure is reported by that caller
        # against the right file, and the worst case is the behaviour this
        # function had before #589.
        #
        # `test_every_typed_reader_reads_the_alias_file_itself` derives the
        # caller set from the source and checks that property, so a new caller
        # that skipped the alias read would redden rather than silently make
        # this comment false. An earlier draft said "all eight, checked" -- it
        # is seven; the eighth was `create_app`, counted because
        # `_source_trust_rollup` is nested inside it. That number licensed
        # nothing the property does not, and it was wrong.
        return specs
    _refuse_canonical_collisions(specs, aliases)
    return specs


def _refuse_canonical_collisions(
    typed: dict[str, TypedRelationSpec], aliases: dict[str, str]
) -> None:
    """Refuse two declarations that canonicalise to one relation (#589).

    WHY HERE AND NOT IN THE RESOLVER. Before this change the two were separate
    keys and both were ignored, so there was nothing to disambiguate; resolving
    them makes dict order decide which declaration takes effect, which is the
    defect class #589 exists to remove. But refusing it inside the resolver made
    the error a NEW raiser, below every guard the trust path has: measured, six
    of eight routes answered 500, while this file's existing duplicate-alias
    refusal -- raised from `typed_relations`, one frame up from here -- leaves
    all eight at 200. Raising from the file read inherits those guards instead
    of needing new ones.

    The WHOLE table is checked rather than the queried relation, so the verdict
    depends on the file alone and not on which facts a page happens to touch.
    """
    seen: dict[str, str] = {}
    for declared in typed:
        key = unicodedata.normalize("NFC", canonical_relation(declared, aliases))
        if key in seen:
            raise CorroborationPolicyError(
                f"typed-relations.md: {seen[key]!r} and {declared!r} "
                f"both declare a type for the relation {key!r}"
            )
        seen[key] = declared


def typed_spec_for_canonical(
    typed: dict[str, TypedRelationSpec],
    canonical: str,
    aliases: dict[str, str],
) -> TypedRelationSpec | None:
    """Resolve a typed declaration for ``canonical``, through the alias table.

    THE ONLY COPY OF THIS LOOKUP -- scoped deliberately, because it is not the
    only place in the package that knows this rule. Every consumer that had
    `typed.get(relation)` open-coded now calls this, because `typed_relations`
    keys the dict by the label the user WROTE while every consumer looks up the
    CANONICAL label -- so a declaration on any label the alias table rewrites
    was stored under a key nobody queries and silently did nothing (#589).

    Two other resolvers exist and BOTH are deliberate, so a reader who finds
    them does not have to guess whether they were missed.
    `query_planner._typed_specs_for_canonical_relation` already resolved this
    way and is the prior art this follows. `query_schema._typed_for_relation`
    is NOT converted: it tries `display`, NFC(display), `canonical` and
    NFC(canonical), which resolves a declaration written under the label the
    fact itself uses but not one under a DIFFERENT raw label sharing the same
    canonical. That residual gap is measured and tracked as #597, left out of
    #589 on purpose because that snapshot feeds the LLM planner's prompts.

    The keys stay as written on purpose, and for ONE reason rather than the two
    an earlier draft of this docstring gave. It said canonicalising them inside
    `store_typed_relations` would also need the alias table in a signature the
    CLI and the schema snapshot share; that stopped being true when the
    collision check moved there -- it reads the alias table now, and needed no
    signature change, because that function already takes the `Store`. The
    reason that survives is the other one: canonical keys would leave the stored
    dict disagreeing with the user's file, so a diagnostic could no longer quote
    their own line back to them.

    THIS FUNCTION DOES NOT RAISE, and that is load-bearing rather than
    incidental. Ambiguity -- two labels canonicalising to one relation -- is
    refused, but by `_refuse_canonical_collisions` at the point the FILE is
    read. Refusing it here instead raised a `CorroborationPolicyError` from a
    NEW raiser, one that every existing guard sits above rather than below:
    measured, six of eight routes answered 500 where the file's own
    duplicate-alias refusal leaves all eight at 200. Raising from
    `store_typed_relations` inherits the guards that already exist for that
    refusal; raising from here would have needed a sixth guard rollout to
    reach the same place.
    """
    want = unicodedata.normalize("NFC", canonical)
    for declared, spec in typed.items():
        if unicodedata.normalize("NFC", canonical_relation(declared, aliases)) == want:
            return spec
    return None


def corroboration(
    facts: Iterable[Mapping[str, object]],
    aliases: dict[str, str] | None = None,
) -> list[FactSupport]:
    """Return distinct-source support for confirmed/accepted SPO triples.

    The grouping key canonicalizes the relation, like `single_valued_conflicts`,
    so two sources that used different raw aliases for the same relation ("설립"
    and "founded") still merge into one corroborated group now that facts are
    stored with their raw labels (#252).
    """
    aliases = aliases or {}
    sources: dict[tuple[str, str, str], set[str]] = {}
    for row in facts:
        if not is_engine_input(_value(row, "status", "")):
            continue
        source = _source_ref(row)
        if not source:
            continue
        relation = _canonical_relation(str(row["relation"]), aliases)
        key = (str(row["subject"]), relation, str(row["object"]))
        sources.setdefault(key, set()).add(source)
    return [
        FactSupport(subject=s, relation=r, object=o, sources=tuple(sorted(srcs)))
        for (s, r, o), srcs in sorted(sources.items())
    ]


def single_valued_conflicts(
    facts: Iterable[Mapping[str, object]],
    single_valued: set[str],
    aliases: dict[str, str] | None = None,
    typed: dict[str, TypedRelationSpec] | None = None,
) -> list[SingleValuedConflict]:
    """Return conflicting values for single-valued relations with source support."""
    aliases = aliases or {}
    typed = typed or {}
    canonical_single_valued = {_canonical_relation(r, aliases) for r in single_valued}
    by_subject_relation: dict[
        tuple[str, str], dict[tuple[str, object], dict[str, set[str]]]
    ] = {}
    for row in facts:
        if not is_engine_input(_value(row, "status", "")):
            continue
        relation = _canonical_relation(str(row["relation"]), aliases)
        if relation not in canonical_single_valued:
            continue
        # #589. This site produces the conflict count -- and the /sources
        # `conflicted` badge through `store_single_valued_conflicts`.
        spec = typed_spec_for_canonical(typed, relation, aliases)
        source = _source_ref(row)
        if not source:
            continue
        key = (str(row["subject"]), relation)
        obj = str(row["object"])
        object_key = _object_group_key(obj, spec)
        by_subject_relation.setdefault(key, {}).setdefault(object_key, {}).setdefault(
            obj, set()
        ).add(source)

    conflicts: list[SingleValuedConflict] = []
    for (subject, relation), groups in sorted(by_subject_relation.items()):
        if len(groups) < 2:
            continue
        values = []
        for raws in groups.values():
            representative = sorted(raws)[0]
            sources = set().union(*raws.values())
            values.append(
                CompetingValue(object=representative, sources=tuple(sorted(sources)))
            )
        conflicts.append(
            SingleValuedConflict(
                subject=subject,
                relation=relation,
                values=tuple(sorted(values, key=lambda value: value.object)),
            )
        )
    return conflicts


def store_corroboration(store: Store) -> list[FactSupport]:
    return corroboration(store.facts(), store_relation_aliases(store))


def store_single_valued_conflicts(store: Store) -> list[SingleValuedConflict]:
    return single_valued_conflicts(
        store.facts(),
        store_functional_relations(store),
        store_relation_aliases(store),
        store_typed_relations(store),
    )


def _source_ref(row: Mapping[str, object]) -> str:
    value = _value(row, "source_path", "") or _value(row, "source", "")
    return str(value).strip()


def _canonical_relation(relation: str, aliases: dict[str, str]) -> str:
    return canonical_relation(relation, aliases)


def canonical_relation(relation: str, aliases: dict[str, str]) -> str:
    """Return the relation name used for alias-aware trust comparisons."""
    return relation_canonical_variant(relation, aliases)


def relation_canonical_variant(relation: str, aliases: Mapping[str, str]) -> str:
    """Return the alias canonical label for ``relation`` when policy defines one."""
    if not aliases:
        return relation
    return canonical_relation_from_normalized(
        relation, normalized_relation_aliases(aliases)
    )


def normalized_relation_aliases(aliases: Mapping[str, str]) -> dict[str, str]:
    """NFC-normalize an alias table once, for callers that map many labels."""
    return {
        unicodedata.normalize("NFC", raw): unicodedata.normalize("NFC", canonical)
        for raw, canonical in aliases.items()
    }


def canonical_relation_from_normalized(
    relation: str, normalized_aliases: Mapping[str, str]
) -> str:
    """Canonicalize one label against an already NFC-normalized alias table."""
    if not normalized_aliases:
        return relation
    normalized = unicodedata.normalize("NFC", relation)
    if normalized in normalized_aliases:
        return normalized_aliases[normalized]
    if normalized in set(normalized_aliases.values()):
        return normalized
    return relation


def relation_label_variants(relation: str, aliases: Mapping[str, str]) -> tuple[str, ...]:
    """Return deterministic alias-equivalent labels for a relation label.

    The input label is preserved as the first variant after NFC normalization so
    callers that render queries keep observed labels ahead of policy-derived
    alternatives.
    """
    normalized = unicodedata.normalize("NFC", relation)
    variants = [normalized]
    canonical = relation_canonical_variant(normalized, aliases)
    if canonical != normalized:
        variants.append(canonical)
    normalized_aliases = {
        unicodedata.normalize("NFC", raw): unicodedata.normalize("NFC", target)
        for raw, target in aliases.items()
    }
    for alias, target in sorted(
        normalized_aliases.items(),
        key=lambda item: (
            item[1],
            item[0],
        ),
    ):
        if target == canonical and alias not in variants:
            variants.append(alias)
    return tuple(variants)


def relation_label_matches(
    observed: str, wanted: str, aliases: Mapping[str, str]
) -> bool:
    """Return whether two relation labels match under alias/canonical semantics."""
    observed_variants = set(relation_label_variants(observed, aliases))
    wanted_variants = set(relation_label_variants(wanted, aliases))
    return not observed_variants.isdisjoint(wanted_variants)


def _value(row: Mapping[str, object], key: str, default: object = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _object_group_key(obj: str, spec: TypedRelationSpec | None) -> tuple[str, object]:
    if spec is not None:
        scalar = normalize_typed_value(spec.type, obj, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", obj)


def normalize_typed_value(
    type_tag: str, raw: str, units: dict[str, int] | None = None
) -> int | None:
    if type_tag == "date":
        return _parse_date(raw)
    if type_tag == "number":
        return _parse_number_scaled(raw)
    if type_tag == "ordinal":
        return _parse_ordinal(raw)
    if type_tag == "amount":
        return _parse_amount(raw, units or _DEFAULT_AMOUNT_UNITS)
    return None


def _parse_date(raw: str) -> int | None:
    match = _DATE_COMPOUND_RE.match(raw.strip()) or _DATE_RE.match(raw.strip())
    if match is None:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3)) if match.group(3) is not None else 1
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return year * 10000 + month * 100 + day


def _parse_number_scaled(raw: str) -> int | None:
    text = raw.strip()
    compound = _NUMBER_COMPOUND_RE.match(text)
    if compound is not None:
        text = compound.group(1)
    if _NUMBER_RE.match(text) is None:
        return None
    try:
        product = Decimal(text.replace(",", "")) * _NUMBER_SCALE
    except decimal.InvalidOperation:
        return None
    if product == product.to_integral_value():
        return int(product)
    return int(product.to_integral_value(rounding=decimal.ROUND_HALF_UP))


def _parse_ordinal(raw: str) -> int | None:
    match = (
        _ORDINAL_COMPOUND_RE.match(raw.strip())
        or _ORDINAL_KO_RE.match(raw.strip())
        or _ORDINAL_EN_RE.match(raw.strip())
    )
    return int(match.group(1)) if match else None


def _parse_amount(raw: str, units: dict[str, int]) -> int | None:
    text = raw.strip()
    match = _AMOUNT_COMPOUND_RE.match(text)
    if match is not None:
        unit = (match.groupdict().get("qunit") or match.group("unit")).strip()
    else:
        match = _AMOUNT_RE.match(text)
        if match is None:
            return None
        unit = match.group("unit").strip()
    multiplier = units.get(unit)
    if multiplier is None and unit.endswith(_CURRENCY_MARKER):
        multiplier = units.get(unit[: -len(_CURRENCY_MARKER)])
    if multiplier is None:
        return None
    try:
        product = Decimal(match.group("num").replace(",", "")) * multiplier
    except decimal.InvalidOperation:
        return None
    if product == product.to_integral_value():
        value = int(product)
    else:
        value = int(product.to_integral_value(rounding=decimal.ROUND_HALF_UP))
    if value < _INT64_MIN or value > _INT64_MAX:
        return None
    return value


def _parse_amount_units(body: str) -> dict[str, int]:
    units: dict[str, int] = {}
    for pair in body.split(","):
        if not pair.strip():
            continue
        unit, sep, value = pair.partition("=")
        if not sep:
            raise CorroborationPolicyError(
                f"typed-relations.md: malformed unit pair {pair!r}"
            )
        unit = unit.strip()
        value = value.strip()
        try:
            number = decimal.Decimal(value)
        except decimal.InvalidOperation as exc:
            raise CorroborationPolicyError(
                f"typed-relations.md: non-numeric unit value {value!r}"
            ) from exc
        if (
            not unit
            or not number.is_finite()
            or number != number.to_integral_value()
            or number <= 0
        ):
            raise CorroborationPolicyError(
                f"typed-relations.md: invalid unit mapping {pair!r}"
            )
        units[unit] = int(number)
    return units
