# SPDX-License-Identifier: MPL-2.0
"""Explicit fact input boundary helpers.

Plain Python strings passed to `Store.add_fact` / `Store.amend_fact` are stored
as `StringLit` terms. Call `structural_term(...)` only when the caller
intentionally wants Datalog term syntax parsed as a logical term.
"""

from __future__ import annotations

from verinote.engine.terms import (
    Compound,
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


def term_input_kind(term: Term) -> str:
    """Return the edit/input kind for a stored term."""
    return "string" if isinstance(term, StringLit) else "term"


def _has_var(term: Term) -> bool:
    if isinstance(term, Var):
        return True
    if isinstance(term, Compound):
        return any(_has_var(arg) for arg in term.args)
    return False
