# SPDX-License-Identifier: MPL-2.0
"""Explicit fact input boundary helpers.

Plain Python strings passed to `Store.add_fact` / `Store.amend_fact` are stored
as `StringLit` terms. Call `structural_term(...)` only when the caller
intentionally wants Datalog term syntax parsed as a logical term.
"""

from __future__ import annotations

import math

from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    Term,
    TermParseError,
    Var,
    parse_term,
)
from verinote.text import nfc


def structural_term(text: str) -> Term:
    """Parse explicit structural fact-term syntax.

    Invalid syntax raises `TermParseError`. Variables are rejected because base
    fact rows are data, not Datalog rule patterns.
    """
    term = parse_term(text)
    if not is_ground_term(term):
        raise TermParseError("structural fact terms must be ground")
    return term


def nfc_term(term: Term) -> Term:
    """Normalize every StringLit leaf in `term` to NFC, recursively.

    Only StringLit can hold non-ASCII text; Compound recurses, everything else
    passes through unchanged. Must run AFTER parsing, not before: a term's
    source text can be pure ASCII (e.g. \\uXXXX-escaped) yet decode to
    non-NFC codepoints, which whole-string normalization before parsing would
    never touch (#200).
    """
    if isinstance(term, StringLit):
        return StringLit(nfc(term.value))
    if isinstance(term, Compound):
        return Compound(term.functor, tuple(nfc_term(a) for a in term.args))
    return term


def is_ground_term(term: Term) -> bool:
    """Return True when a term contains no variables."""
    return not _has_var(term)


def validate_fact_slots(
    subject: object, relation: object, obj: object
) -> tuple[Term, Term, Term]:
    """Validate and normalize one fact triple into engine terms.

    Plain strings are deliberate data strings, not term syntax. Every other
    accepted value must already be a concrete, ground engine term.
    """
    return (
        validate_fact_slot(subject),
        validate_fact_slot(relation),
        validate_fact_slot(obj),
    )


def validate_fact_slot(value: object) -> Term:
    """Validate and normalize one fact slot into an engine term."""
    if isinstance(value, str):
        return StringLit(value)
    if isinstance(value, Compound):
        _validate_fact_term(value)
        return value
    if isinstance(value, (Atom, NumberLit, StringLit)):
        _validate_fact_term(value)
        return value
    if isinstance(value, Var):
        raise ValueError("fact terms must be ground")
    raise ValueError("fact slots must be plain strings or ground engine terms")


def validate_fact_confidence(confidence: object) -> float:
    """Validate a fact confidence before it reaches any storage backend."""
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("fact confidence must be a finite number between 0 and 1")
    if not 0.0 <= confidence <= 1.0 or not math.isfinite(confidence):
        raise ValueError("fact confidence must be a finite number between 0 and 1")
    return float(confidence)


def term_input_kind(term: Term) -> str:
    """Return the edit/input kind for a stored term."""
    return "string" if isinstance(term, StringLit) else "term"


def _has_var(term: Term) -> bool:
    if isinstance(term, Var):
        return True
    if isinstance(term, Compound):
        return any(_has_var(arg) for arg in term.args)
    return False


def _validate_fact_term(term: Term, active_compounds: set[int] | None = None) -> None:
    """Reject non-ground terms and terms whose public invariants do not hold."""
    if isinstance(term, StringLit):
        if not isinstance(term.value, str):
            raise ValueError("fact StringLit values must be strings")
        return
    if isinstance(term, Atom):
        _validate_term_invariant(lambda: Atom(term.name), "Atom")
        return
    if isinstance(term, NumberLit):
        _validate_term_invariant(lambda: NumberLit(term.value), "NumberLit")
        return
    if isinstance(term, Var):
        raise ValueError("fact terms must be ground")
    if isinstance(term, Compound):
        _validate_term_invariant(lambda: Compound(term.functor, term.args), "Compound")
        if active_compounds is None:
            active_compounds = set()
        compound_id = id(term)
        if compound_id in active_compounds:
            raise ValueError("fact terms must be acyclic")
        active_compounds.add(compound_id)
        try:
            for arg in term.args:
                _validate_fact_term(arg, active_compounds)
        finally:
            active_compounds.remove(compound_id)


def _validate_term_invariant(make_term, term_name: str) -> None:
    try:
        make_term()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid fact {term_name}") from exc
