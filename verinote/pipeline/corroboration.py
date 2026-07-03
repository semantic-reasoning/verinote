# SPDX-License-Identifier: MPL-2.0
"""Source-support and single-valued conflict views for engine-input facts.

Borrowed from factlog's deterministic trust signals: distinct source support is
reported separately from LLM confidence, and single-valued conflicts are judged
only over facts that have crossed the review gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable, Mapping

from verinote.engine import DEFAULT_POLICY
from verinote.store import ENGINE_STATUSES, Store

_FUNCTIONAL_RE = re.compile(r'functional\("((?:\\.|[^"\\])*)"\)\.')
_ALIAS_RE = re.compile(r"`([^`]+)`\s*->\s*`([^`]+)`")
RELATION_ALIASES_RELPATH = "policy/relation-aliases.md"


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
    for line in text.splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ALIAS_RE.fullmatch(stripped)
        if match is None:
            continue
        raw = unicodedata.normalize("NFC", match.group(1).strip())
        canonical = unicodedata.normalize("NFC", match.group(2).strip())
        if not raw or not canonical:
            continue
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


def store_relation_aliases(store: Store) -> dict[str, str]:
    path = store.db_path.parent / RELATION_ALIASES_RELPATH
    if not path.is_file():
        return {}
    return relation_aliases(path.read_text(encoding="utf-8"))


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
) -> list[SingleValuedConflict]:
    """Return conflicting values for single-valued relations with source support."""
    aliases = aliases or {}
    canonical_single_valued = {_canonical_relation(r, aliases) for r in single_valued}
    by_subject_relation: dict[tuple[str, str], dict[str, set[str]]] = {}
    for row in facts:
        if str(_value(row, "status", "")) not in ENGINE_STATUSES:
            continue
        relation = _canonical_relation(str(row["relation"]), aliases)
        if relation not in canonical_single_valued:
            continue
        source = _source_ref(row)
        if not source:
            continue
        key = (str(row["subject"]), relation)
        by_subject_relation.setdefault(key, {}).setdefault(
            str(row["object"]), set()
        ).add(source)

    conflicts: list[SingleValuedConflict] = []
    for (subject, relation), values in sorted(by_subject_relation.items()):
        if len(values) < 2:
            continue
        conflicts.append(
            SingleValuedConflict(
                subject=subject,
                relation=relation,
                values=tuple(
                    CompetingValue(object=obj, sources=tuple(sorted(srcs)))
                    for obj, srcs in sorted(values.items())
                ),
            )
        )
    return conflicts


def store_corroboration(store: Store) -> list[FactSupport]:
    return corroboration(store.facts())


def store_single_valued_conflicts(store: Store) -> list[SingleValuedConflict]:
    return single_valued_conflicts(
        store.facts(), store_functional_relations(store), store_relation_aliases(store)
    )


def _source_ref(row: Mapping[str, object]) -> str:
    value = _value(row, "source_path", "") or _value(row, "source", "")
    return str(value).strip()


def _canonical_relation(relation: str, aliases: dict[str, str]) -> str:
    if not aliases:
        return relation
    normalized = unicodedata.normalize("NFC", relation)
    if normalized in aliases:
        return aliases[normalized]
    if normalized in set(aliases.values()):
        return normalized
    return relation


def _value(row: Mapping[str, object], key: str, default: object = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)
