# SPDX-License-Identifier: MPL-2.0
"""The finding shapes verinote declares itself, and the one place they are named.

A policy is the user's to write (#159), so a derived finding's columns mean
whatever its author meant. The engine sees a tuple of symbols: nothing in
`error_mentions(S, O) :- relation(S, "mentions", O)` tells it that `O` is an
object and not a relation. Any reader that wants to know *which* subject or
*which* relation a finding is about therefore has to restrict itself to the
rules verinote itself declares, where the column order is a thing verinote
decided rather than a thing it guessed.

`error_functional_conflict` is such a rule: `DEFAULT_POLICY` (wirelog.py) ships
it, `verinote init` scaffolds it, and it is declared right here — the decl line
in that policy is rendered from these constants, so the generator and the
readers cannot drift apart.

The name alone is not the contract, because the scaffolded policy is a *copy* a
KB may edit: a user is free to declare `error_functional_conflict` with a shape
of their own, and then the columns are theirs and not ours. So a reader matches
name *and* declared columns, and treats anything else as a rule whose shape it
does not know.
"""

from __future__ import annotations

__all__ = [
    "FUNCTIONAL_CONFLICT_COLUMNS",
    "FUNCTIONAL_CONFLICT_DECL",
    "FUNCTIONAL_CONFLICT_RULE",
    "functional_conflict_target",
]

#: verinote's own "this relation may hold at most one object per subject" rule.
FUNCTIONAL_CONFLICT_RULE = "error_functional_conflict"

#: Its head columns, in order: `values[0]` is the subject, `values[1]` the
#: relation. This ordering is the whole reason a note can be attached at all.
FUNCTIONAL_CONFLICT_COLUMNS = ("subject", "rel")

#: The declaration `DEFAULT_POLICY` ships, rendered from the constants above so
#: that a shape change cannot reach the policy without reaching the readers.
FUNCTIONAL_CONFLICT_DECL = (
    f".decl {FUNCTIONAL_CONFLICT_RULE}("
    + ", ".join(f"{column}: symbol" for column in FUNCTIONAL_CONFLICT_COLUMNS)
    + ")"
)


def functional_conflict_target(
    rule: str, columns: tuple[str, ...], values: tuple[str, ...]
) -> tuple[str, str] | None:
    """The (subject, relation) a functional-conflict finding is about, or None.

    None means "not verinote's rule, or not the shape verinote declared" — the
    caller then knows nothing about what these columns mean and must not read
    them positionally. Arity is re-checked against the declaration because the
    values come from the engine and the columns from the policy text.
    """
    if rule != FUNCTIONAL_CONFLICT_RULE or columns != FUNCTIONAL_CONFLICT_COLUMNS:
        return None
    if len(values) != len(FUNCTIONAL_CONFLICT_COLUMNS):
        return None
    return values[0], values[1]
