# SPDX-License-Identifier: MPL-2.0
"""Deterministic query-schema snapshots for engine-input KB facts.

The snapshot intentionally uses only confirmed/accepted facts and keeps two
identities side by side: source/display labels from SQLite and executable terms
from the DuckDB fact-term sidecar. Policy metadata for aliases and typed
relations is intentionally unbounded because it is authored configuration, not
observed KB data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    Term,
    canonical_term_key,
    render_term,
)
from verinote.pipeline.corroboration import (
    TypedRelationSpec,
    canonical_relation,
    store_relation_aliases,
    store_typed_relations,
)
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.store import Store, engine_statuses
from verinote.text import nfc as _nfc


@dataclass(frozen=True)
class QuerySchemaBounds:
    max_relations: int = 100
    max_entities_per_side: int = 100
    max_exact_entity_facts: int = 50
    max_join_facts: int = 500

    def __post_init__(self) -> None:
        for field in (
            "max_relations",
            "max_entities_per_side",
            "max_exact_entity_facts",
            "max_join_facts",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")


@dataclass(frozen=True)
class TermRef:
    display: str
    executable: str
    kind: str
    key: str


@dataclass(frozen=True)
class EntityRef:
    display: str
    executable: str
    kind: str
    key: str
    fact_count: int


@dataclass(frozen=True)
class RelationAliasEntry:
    alias: str
    canonical: str


@dataclass(frozen=True)
class UnitScale:
    unit: str
    scale: int


@dataclass(frozen=True)
class TypedRelationEntry:
    relation: str
    type: str
    alias: str
    units: tuple[UnitScale, ...] = ()


@dataclass(frozen=True)
class SnapshotFact:
    fact_id: int
    subject: TermRef
    relation: TermRef
    object: TermRef
    status: str
    matched_entity: str | None = None
    matched_side: str | None = None


@dataclass(frozen=True)
class RelationSchema:
    relation: TermRef
    canonical_relation: str
    aliases: tuple[RelationAliasEntry, ...]
    typed: TypedRelationEntry | None
    fact_count: int
    distinct_subject_count: int
    distinct_object_count: int
    subjects: tuple[EntityRef, ...]
    objects: tuple[EntityRef, ...]
    subjects_truncated: bool
    objects_truncated: bool


@dataclass(frozen=True)
class QuerySchemaSnapshot:
    relations: tuple[RelationSchema, ...]
    relations_truncated: bool
    relation_aliases: tuple[RelationAliasEntry, ...]
    typed_relations: tuple[TypedRelationEntry, ...]
    exact_entity_facts: tuple[SnapshotFact, ...]
    exact_entity_facts_truncated: bool
    fact_count: int
    join_facts: tuple[SnapshotFact, ...] = ()
    """Complete engine-input fact rows used only for bounded join planning."""
    join_facts_truncated: bool = False
    all_relation_labels: frozenset[str] | None = None
    """Every relation surface in the KB, NFC-folded and *not* truncated.

    The lists above are bounded because they are rendered -- into the model's
    schema hint and into candidate generation -- so a large KB must not blow
    either up. Membership is a different question: answering "does this label
    exist at all?" from a list capped at `max_relations` reports a relation the
    KB holds as absent, which is worse than not answering. These sets are
    derived from the same complete fact list the bounded views are taken from
    (`_engine_facts` reads every engine-status fact), so they cost no extra
    query and carry no truncation.

    `None` means the snapshot was built without them -- by a caller constructing
    one directly rather than through `build_query_schema_snapshot` -- and
    absence is then unknown rather than false. Diagnosis is skipped instead of
    guessed.
    """
    all_entity_surfaces: frozenset[str] | None = None
    """Every subject and object surface in the KB, NFC-folded and not truncated.

    Carries each term's `display`, `executable` and `key` spellings, because
    that is the set `_matching_entities` compares an intent's entity against.
    See `all_relation_labels` for why this is unbounded and what `None` means.
    """


@dataclass(frozen=True)
class _Fact:
    fact_id: int
    subject: TermRef
    relation: TermRef
    object: TermRef
    status: str


def build_query_schema_snapshot(
    store: Store,
    *,
    exact_entities: Iterable[str] = (),
    bounds: QuerySchemaBounds = QuerySchemaBounds(),
    include_typed_relations: bool = True,
    include_join_facts: bool = False,
) -> QuerySchemaSnapshot:
    """Build a deterministic schema snapshot for future query planning."""
    aliases = store_relation_aliases(store)
    typed_specs = store_typed_relations(store) if include_typed_relations else {}
    facts = _engine_facts(store)

    alias_entries = _alias_entries(aliases)
    typed_entries = _typed_entries(typed_specs)
    relation_rows = _relation_schemas(facts, aliases, typed_specs, bounds)
    exact_rows = _exact_entity_facts(facts, exact_entities, bounds)
    join_rows = tuple(
        SnapshotFact(
            fact_id=fact.fact_id,
            subject=fact.subject,
            relation=fact.relation,
            object=fact.object,
            status=fact.status,
        )
        for fact in sorted(facts, key=_fact_sort_key)[: bounds.max_join_facts]
    ) if include_join_facts else ()

    return QuerySchemaSnapshot(
        relations=relation_rows[: bounds.max_relations],
        relations_truncated=len(relation_rows) > bounds.max_relations,
        relation_aliases=alias_entries,
        typed_relations=typed_entries,
        exact_entity_facts=exact_rows[: bounds.max_exact_entity_facts],
        exact_entity_facts_truncated=len(exact_rows) > bounds.max_exact_entity_facts,
        fact_count=len(facts),
        join_facts=join_rows,
        join_facts_truncated=include_join_facts and len(facts) > bounds.max_join_facts,
        all_relation_labels=_all_relation_labels(facts, typed_entries),
        all_entity_surfaces=_all_entity_surfaces(facts),
    )


def _engine_facts(store: Store) -> list[_Fact]:
    display_rows = {
        int(row["id"]): row for row in store.facts(statuses=engine_statuses())
    }
    term_rows = {int(row["id"]): row for row in engine_relation_rows(store)}
    facts: list[_Fact] = []
    for fact_id in sorted(display_rows):
        row = display_rows[fact_id]
        term_row = term_rows[fact_id]
        facts.append(
            _Fact(
                fact_id=fact_id,
                subject=_term_ref(row["subject"], term_row["subject"]),
                # The stored relation label, not the alias canonical the engine
                # is fed: a planner reading this snapshot should write a query in
                # the KB's own words, and `expand_query_relation_aliases` adds
                # the canonical rule for it. The alias table is reported
                # separately, so the canonical is not hidden from the planner.
                relation=_term_ref(row["relation"], term_row["relation_raw"]),
                object=_term_ref(row["object"], term_row["object"]),
                status=str(row["status"]),
            )
        )
    return facts


def _relation_schemas(
    facts: list[_Fact],
    aliases: Mapping[str, str],
    typed_specs: Mapping[str, TypedRelationSpec],
    bounds: QuerySchemaBounds,
) -> tuple[RelationSchema, ...]:
    by_relation: dict[tuple[str, str, str, str], list[_Fact]] = {}
    for fact in facts:
        by_relation.setdefault(_term_identity(fact.relation), []).append(fact)

    rows: list[RelationSchema] = []
    for identity in sorted(by_relation, key=_relation_identity_sort_key):
        relation_facts = sorted(by_relation[identity], key=_fact_sort_key)
        display = relation_facts[0].relation.display
        canonical = canonical_relation(display, dict(aliases))
        subjects = _entity_examples(
            (fact.subject for fact in relation_facts), bounds.max_entities_per_side
        )
        objects = _entity_examples(
            (fact.object for fact in relation_facts), bounds.max_entities_per_side
        )
        all_subjects = _entity_counts(fact.subject for fact in relation_facts)
        all_objects = _entity_counts(fact.object for fact in relation_facts)
        rows.append(
            RelationSchema(
                relation=relation_facts[0].relation,
                canonical_relation=canonical,
                aliases=_aliases_for_relation(display, canonical, aliases),
                typed=_typed_for_relation(display, canonical, typed_specs),
                fact_count=len(relation_facts),
                distinct_subject_count=len(all_subjects),
                distinct_object_count=len(all_objects),
                subjects=subjects,
                objects=objects,
                subjects_truncated=len(all_subjects) > bounds.max_entities_per_side,
                objects_truncated=len(all_objects) > bounds.max_entities_per_side,
            )
        )
    return tuple(rows)


def _exact_entity_facts(
    facts: list[_Fact],
    exact_entities: Iterable[str],
    bounds: QuerySchemaBounds,
) -> tuple[SnapshotFact, ...]:
    wanted = tuple(dict.fromkeys(str(entity) for entity in exact_entities))
    if not wanted:
        return ()
    matched: list[SnapshotFact] = []
    for fact in sorted(facts, key=_fact_sort_key):
        sides: list[str] = []
        matched_entity: str | None = None
        for exact in wanted:
            exact_nfc = _nfc(exact)
            subject_match = _entity_matches(fact.subject, exact_nfc)
            object_match = _entity_matches(fact.object, exact_nfc)
            if subject_match or object_match:
                if subject_match:
                    sides.append("subject")
                if object_match:
                    sides.append("object")
                matched_entity = exact
                break
        if not sides:
            continue
        matched.append(
            SnapshotFact(
                fact_id=fact.fact_id,
                subject=fact.subject,
                relation=fact.relation,
                object=fact.object,
                status=fact.status,
                matched_entity=matched_entity,
                matched_side="both" if len(sides) == 2 else sides[0],
            )
        )
    return tuple(matched[: bounds.max_exact_entity_facts + 1])


def _entity_matches(ref: TermRef, exact_nfc: str) -> bool:
    if ref.kind == "StringLit" and _nfc(ref.display) == exact_nfc:
        return True
    return _nfc(ref.executable) == exact_nfc


def _entity_examples(
    refs: Iterable[TermRef], limit: int
) -> tuple[EntityRef, ...]:
    counts = _entity_counts(refs)
    rows = [
        EntityRef(
            display=ref.display,
            executable=ref.executable,
            kind=ref.kind,
            key=ref.key,
            fact_count=count,
        )
        for ref, count in counts.values()
    ]
    rows.sort(key=lambda row: (_nfc(row.display), _nfc(row.executable), row.kind, row.key))
    return tuple(rows[:limit])


def _entity_counts(refs: Iterable[TermRef]) -> dict[tuple[str, str, str, str], tuple[TermRef, int]]:
    counts: dict[tuple[str, str, str, str], tuple[TermRef, int]] = {}
    for ref in refs:
        key = _term_identity(ref)
        if key not in counts:
            counts[key] = (ref, 0)
        counts[key] = (counts[key][0], counts[key][1] + 1)
    return counts


def _term_identity(ref: TermRef) -> tuple[str, str, str, str]:
    return (ref.display, ref.executable, ref.kind, ref.key)


def _relation_identity_sort_key(
    identity: tuple[str, str, str, str],
) -> tuple[str, str, str, str]:
    display, executable, kind, key = identity
    return (_nfc(display), _nfc(executable), kind, key)


def _aliases_for_relation(
    display: str, canonical: str, aliases: Mapping[str, str]
) -> tuple[RelationAliasEntry, ...]:
    normalized_display = _nfc(display)
    normalized_canonical = _nfc(canonical)
    entries = [
        RelationAliasEntry(alias=alias, canonical=target)
        for alias, target in aliases.items()
        if _nfc(alias) == normalized_display or _nfc(target) == normalized_canonical
    ]
    return tuple(sorted(entries, key=lambda entry: (_nfc(entry.canonical), _nfc(entry.alias))))


def _typed_for_relation(
    display: str,
    canonical: str,
    typed_specs: Mapping[str, TypedRelationSpec],
) -> TypedRelationEntry | None:
    for key in (display, _nfc(display), canonical, _nfc(canonical)):
        spec = typed_specs.get(key)
        if spec is not None:
            return _typed_entry(key, spec)
    return None


def _alias_entries(aliases: Mapping[str, str]) -> tuple[RelationAliasEntry, ...]:
    return tuple(
        RelationAliasEntry(alias=alias, canonical=canonical)
        for alias, canonical in sorted(
            aliases.items(), key=lambda item: (_nfc(item[1]), _nfc(item[0]))
        )
    )


def _typed_entries(
    specs: Mapping[str, TypedRelationSpec]
) -> tuple[TypedRelationEntry, ...]:
    return tuple(
        _typed_entry(relation, spec)
        for relation, spec in sorted(specs.items(), key=lambda item: _nfc(item[0]))
    )


def _typed_entry(relation: str, spec: TypedRelationSpec) -> TypedRelationEntry:
    units = ()
    if spec.units:
        units = tuple(
            UnitScale(unit=unit, scale=scale)
            for unit, scale in sorted(spec.units.items(), key=lambda item: _nfc(item[0]))
        )
    return TypedRelationEntry(
        relation=relation,
        type=spec.type,
        alias=spec.alias,
        units=units,
    )


def _all_relation_labels(
    facts: Iterable[_Fact], typed: Iterable[TypedRelationEntry]
) -> frozenset[str]:
    """Every spelling by which a relation can be requested, for membership only.

    Facts are not the whole story. `_relation_matches_any` also observes a
    relation's typed-spec name and its alias, and those live in
    `policy/typed-relations.md` rather than in any fact -- so a set built from
    facts alone reports a typed alias the planner does match as absent, and the
    reason built on it tells the user to declare something they have declared.
    """
    labels = {
        _nfc(surface)
        for fact in facts
        for surface in (fact.relation.display, fact.relation.executable, fact.relation.key)
    }
    for entry in typed:
        labels.add(_nfc(entry.relation))
        if entry.alias:
            labels.add(_nfc(entry.alias))
    return frozenset(labels)


def _all_entity_surfaces(facts: Iterable[_Fact]) -> frozenset[str]:
    """Every spelling of every subject and object in the KB.

    Both endpoints are collected, not just subjects: this answers "is this a
    real entity in this KB?", and an entity sighted only as an object is just as
    real. What it must never be read as is "this entity can be the subject of
    that relation" -- that is what the absence of a planned candidate says.
    """
    return frozenset(
        _nfc(surface)
        for fact in facts
        for ref in (fact.subject, fact.object)
        for surface in (ref.display, ref.executable, ref.key)
    )


def _term_ref(display: object, term: Term) -> TermRef:
    return TermRef(
        display=str(display),
        executable=render_term(term),
        kind=_term_kind(term),
        key=canonical_term_key(term),
    )


def _term_kind(term: Term) -> str:
    if isinstance(term, Atom):
        return "Atom"
    if isinstance(term, Compound):
        return "Compound"
    if isinstance(term, NumberLit):
        return "NumberLit"
    if isinstance(term, StringLit):
        return "StringLit"
    return type(term).__name__


def _fact_sort_key(fact: _Fact) -> tuple[object, ...]:
    return (
        _nfc(fact.subject.display),
        _nfc(fact.relation.display),
        _nfc(fact.object.display),
        fact.subject.key,
        fact.relation.key,
        fact.object.key,
        fact.fact_id,
    )
