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

That match is only possible where the finding's shape is known, which is the
rules verinote declares itself (`engine.policy_vocabulary`). The rest of a
policy is the user's, its columns mean what its author meant, and those findings
keep the words the engine wrote. The report loses a convenience there; what it
does not do is name the wrong facts.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from verinote.engine import CheckReport, FindingRow
from verinote.engine.policy_vocabulary import functional_conflict_target
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
    relation_rows = list(rows)
    renamed = [row for row in relation_rows if _relation_was_renamed(row)]
    if not renamed or not report.findings:
        return report

    rows_by_finding = _finding_rows(report)
    annotated: list[str] = []
    for finding in report.findings:
        note = _source_note(rows_by_finding.get(finding), relation_rows)
        if not note:
            annotated.append(finding)
            continue
        line = f"{finding} {note}"
        annotated.append(line)
        report.text = _replace_line(report.text, finding, line)
    report.findings = annotated
    return report


def _finding_rows(report: CheckReport) -> dict[str, FindingRow]:
    """Map each finding line to the derived row behind it, when it has exactly one.

    A line whose text two different rows produced is left out: it is one finding
    about two rows, so no single row is *the* answer for it, and a note would
    have to guess.
    """
    rows: dict[str, FindingRow] = {}
    ambiguous: set[str] = set()
    for row in report.finding_rows:
        if row.text in rows and _finding_identity(rows[row.text]) != _finding_identity(row):
            ambiguous.add(row.text)
        rows.setdefault(row.text, row)
    return {text: row for text, row in rows.items() if text not in ambiguous}


def _finding_identity(row: FindingRow) -> tuple[str, ...]:
    return row.identity or row.values


def _source_note(row: FindingRow | None, rows: list[Mapping[str, object]]) -> str:
    """Render the aliased facts a finding is about, or '' when it is about none.

    Two things have to be *known*, not guessed, before a fact may be named.

    First, which column is the subject and which is the relation.
    `functional_conflict_target` answers that only for the rule verinote
    declares itself, and answers None for every other rule — including a rule of
    the user's that reuses the name with a shape of its own. A policy is the
    user's to write (#159), so in general there is no such thing as "the
    relation column" of a finding: `error_mentions(S, O) :- relation(S,
    "mentions", O)` derives a row whose *object* may itself be a relation label,
    and reading it as one attaches the facts of a `설립` row to a finding that
    has nothing to do with them. No note is strictly better than a wrong one:
    the note exists only to make provenance readable, so a note that
    misattributes provenance is worse than the bare line the engine wrote.

    Second, which facts. The match is exact and positional, against the
    finding's own row values, never against its text. The text joins values
    bare, so `Org 2 established_on` contains the label `Org` and a containment
    match hands `Org 2`'s conflict the fact ids of `Org`'s. Position matters for
    the same reason: comparing the values as an unordered bag lets a fact match
    on the wrong axis, and a KB holding a subject named `role` would see
    `role/역할=Unrelated` named as provenance for subject `A`'s `role` conflict.

    A finding with no row behind it (`row` is None) gets no note.
    """
    if row is None:
        return ""
    target = functional_conflict_target(row.rule, row.columns, row.values)
    if target is None:
        return ""
    subject, relation = target
    facts = [
        fact
        for fact in rows
        if _label(fact["subject"]) == subject and _label(fact["relation"]) == relation
    ]
    if not facts or not any(_relation_was_renamed(fact) for fact in facts):
        return ""
    parts = [
        f"{_label(fact['relation_raw'])} #{fact['id']}={_label(fact['object'])}"
        for fact in sorted(facts, key=lambda fact: int(fact["id"]))
    ]
    return "(" + ", ".join(parts) + ")"


def _relation_was_renamed(row: Mapping[str, object]) -> bool:
    return _label(row["relation_raw"]) != _label(row["relation"])


def _label(term: object) -> str:
    """The bare surface of a term, the way the engines record finding values."""
    return bare_label(term)  # type: ignore[arg-type]


def _replace_line(text: str, old: str, new: str) -> str:
    return "\n".join(new if line == old else line for line in text.split("\n"))
