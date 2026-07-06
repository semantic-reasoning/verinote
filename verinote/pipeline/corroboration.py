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

from verinote.engine import DEFAULT_POLICY
from verinote.policy_defaults import (
    DEFAULT_RELATION_ALIASES,
    RELATION_ALIASES_RELPATH,
    TYPED_RELATIONS_RELPATH,
)
from verinote.store import ENGINE_STATUSES, Store

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


def functional_relations(policy_dl: str | None) -> set[str]:
    """Parse ``functional("rel").`` declarations from a policy program."""
    text = DEFAULT_POLICY if policy_dl is None else policy_dl
    return {_unescape(m.group(1)) for m in _FUNCTIONAL_RE.finditer(text)}


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
    """Parse factlog-style ``policy/typed-relations.md`` declarations."""
    specs: dict[str, TypedRelationSpec] = {}
    aliases: dict[str, str] = {}
    for line in text.splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"\s*#.*$", "", stripped).strip()
        match = _TYPED_REL_RE.match(stripped)
        if match is None:
            continue
        name = unicodedata.normalize(
            "NFC", (match.group("qname") or match.group("name")).strip()
        )
        type_tag = match.group("type").strip()
        alias = match.group("alias").strip()
        if type_tag not in _TYPED_TYPES:
            continue
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
    return typed_relations(path.read_text(encoding="utf-8"))


def corroboration(facts: Iterable[Mapping[str, object]]) -> list[FactSupport]:
    """Return distinct-source support for confirmed/accepted SPO triples."""
    sources: dict[tuple[str, str, str], set[str]] = {}
    for row in facts:
        if str(_value(row, "status", "")) not in ENGINE_STATUSES:
            continue
        source = _source_ref(row)
        if not source:
            continue
        key = (str(row["subject"]), str(row["relation"]), str(row["object"]))
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
        if str(_value(row, "status", "")) not in ENGINE_STATUSES:
            continue
        relation = _canonical_relation(str(row["relation"]), aliases)
        if relation not in canonical_single_valued:
            continue
        spec = typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))
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
    return corroboration(store.facts())


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
    normalized = unicodedata.normalize("NFC", relation)
    normalized_aliases = {
        unicodedata.normalize("NFC", raw): unicodedata.normalize("NFC", canonical)
        for raw, canonical in aliases.items()
    }
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
