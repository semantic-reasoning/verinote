# SPDX-License-Identifier: MPL-2.0
"""The one place KB facts become engine input.

`Store.engine_fact_terms()` returns the labels a source actually used; a policy
is written against canonical relation labels (`functional("established_on")`).
The KB's relation aliases are what connect the two, so they have to be applied
*here*, above the engines, or the policy silently never matches — two
contradicting `설립` dates pass a `functional("established_on")` policy clean.

Two consequences worth stating:

* Normalization is read-time. Stored facts keep their raw labels, so no
  migration is needed, existing KBs are covered from the next report on, and
  editing the alias file re-decides every fact without re-extraction.
* It happens in the pipeline, not in `Store` (which must not know about policy)
  and not inside an engine (there are two of them, and they would drift). Every
  engine consumer goes through `engine_relation_rows`, so they cannot disagree
  about what a relation is.

Renaming a relation for the engine would otherwise make the report unreadable: a
KB that only ever wrote `설립` would be told that its `established_on` is in
conflict, in words nobody used, with no way to tell which facts collided. So
every row carries the source's label under `relation_raw`, and
`annotate_source_labels` puts those words back into the findings — matched to
each finding's own structured row (`CheckReport.finding_rows`), since a finding
line renders its values bare and cannot be parsed back into fields.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from verinote.engine import CheckReport
from verinote.engine.terms import Atom, StringLit, bare_label
from verinote.pipeline.corroboration import (
    canonical_relation_from_normalized,
    normalized_relation_aliases,
    store_relation_aliases,
)
from verinote.store import Store

__all__ = [
    "annotate_source_labels",
    "canonical_relation_term",
    "engine_relation_rows",
]


def engine_relation_rows(store: Store) -> list[dict[str, object]]:
    """Return engine-input facts with relations canonicalized by KB aliases."""
    aliases = normalized_relation_aliases(store_relation_aliases(store))
    rows: list[dict[str, object]] = []
    for row in store.engine_fact_terms():
        raw = row["relation"]
        rows.append(
            {
                **row,
                "relation": canonical_relation_term(raw, aliases),
                "relation_raw": raw,
            }
        )
    return rows


def canonical_relation_term(
    relation: object, normalized_aliases: Mapping[str, str]
) -> object:
    """Map a relation term onto its alias canonical label, preserving term kind.

    `normalized_aliases` is an alias table already NFC-normalized by
    `normalized_relation_aliases`, so canonicalizing a whole KB does not rebuild
    it once per fact.

    Only textual relation terms carry natural-language labels, which is what an
    alias renames. A structural term (a compound relation) is already logic, not
    a source's wording, so it is passed through untouched.
    """
    if isinstance(relation, StringLit):
        canonical = canonical_relation_from_normalized(
            relation.value, normalized_aliases
        )
        return relation if canonical == relation.value else StringLit(canonical)
    if isinstance(relation, Atom):
        canonical = canonical_relation_from_normalized(
            relation.name, normalized_aliases
        )
        if canonical == relation.name:
            return relation
        try:
            return Atom(canonical)
        except ValueError:
            # A canonical label that is not a valid atom name (spaces, Hangul,
            # …) cannot stay an Atom; keep it a term of the same textual value.
            return StringLit(canonical)
    return relation


def annotate_source_labels(
    report: CheckReport, rows: Iterable[Mapping[str, object]]
) -> CheckReport:
    """Say, in the source's own words, which facts a renamed relation came from.

    A finding reads `functional_conflict: 회사 established_on` while the KB only
    ever said `설립`, and it names no facts. For each finding about a subject and
    an alias-produced canonical relation, this appends the facts behind it as
    `(설립 #1=2020, founded #2=2021)`: the label each fact actually used, its id,
    and its value. Findings for relations the aliases did not rename are left
    exactly as the engine wrote them.
    """
    renamed = [
        row for row in rows if _label(row["relation_raw"]) != _label(row["relation"])
    ]
    if not renamed or not report.findings:
        return report

    values_by_finding = _finding_values(report)
    annotated: list[str] = []
    for finding in report.findings:
        note = _source_note(values_by_finding.get(finding), renamed)
        if not note:
            annotated.append(finding)
            continue
        line = f"{finding} {note}"
        annotated.append(line)
        report.text = _replace_line(report.text, finding, line)
    report.findings = annotated
    return report


def _finding_values(report: CheckReport) -> dict[str, tuple[str, ...]]:
    """Map each finding line to the derived row behind it, when it has exactly one.

    A line whose text two different rows produced is left out: it is one finding
    about two rows, so no single row is *the* answer for it, and a note would
    have to guess.
    """
    values: dict[str, tuple[str, ...]] = {}
    ambiguous: set[str] = set()
    for row in report.finding_rows:
        if row.text in values and values[row.text] != row.values:
            ambiguous.add(row.text)
        values.setdefault(row.text, row.values)
    return {text: vals for text, vals in values.items() if text not in ambiguous}


def _source_note(
    values: tuple[str, ...] | None, renamed: list[Mapping[str, object]]
) -> str:
    """Render the aliased facts a finding is about, or '' when it is about none.

    Matching is exact, against the finding's own row values -- never against its
    text. The text joins values bare, so `Org 2 established_on` contains the
    label `Org`, and a containment match hands `Org 2`'s conflict the fact ids
    and values of `Org`'s: the report then misattributes provenance, which is
    the one thing the note exists to get right. A finding with no row behind it
    (`values` is None) gets no note.

    A row's fields are compared as a set, not by position, because the position
    of a subject or a relation is a property of the policy's rule, which is the
    user's to write. Exactness is what this guarantees: a fact is named only if
    the finding really is about that subject and that relation.
    """
    if values is None:
        return ""
    fields = set(values)
    facts = [
        row
        for row in renamed
        if _label(row["relation"]) in fields and _label(row["subject"]) in fields
    ]
    if not facts:
        return ""
    parts = [
        f"{_label(row['relation_raw'])} #{row['id']}={_label(row['object'])}"
        for row in sorted(facts, key=lambda row: int(row["id"]))
    ]
    return "(" + ", ".join(parts) + ")"


def _label(term: object) -> str:
    """The bare surface of a term, the way the engines record finding values."""
    return bare_label(term)  # type: ignore[arg-type]


def _replace_line(text: str, old: str, new: str) -> str:
    return "\n".join(new if line == old else line for line in text.split("\n"))
