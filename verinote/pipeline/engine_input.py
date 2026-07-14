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

The raw label is preserved under `relation_raw`: what a human reads must stay
the words the source used.
"""

from __future__ import annotations

from typing import Mapping

from verinote.engine.terms import Atom, StringLit
from verinote.pipeline.corroboration import (
    relation_canonical_variant,
    store_relation_aliases,
)
from verinote.store import Store

__all__ = ["canonical_relation_term", "engine_relation_rows"]


def engine_relation_rows(store: Store) -> list[dict[str, object]]:
    """Return engine-input facts with relations canonicalized by KB aliases."""
    aliases = store_relation_aliases(store)
    rows = store.engine_fact_terms()
    normalized: list[dict[str, object]] = []
    for row in rows:
        raw = row["relation"]
        normalized.append(
            {
                **row,
                "relation": canonical_relation_term(raw, aliases),
                "relation_raw": raw,
            }
        )
    return normalized


def canonical_relation_term(relation: object, aliases: Mapping[str, str]) -> object:
    """Map a relation term onto its alias canonical label, preserving term kind.

    Only textual relation terms carry natural-language labels, which is what an
    alias renames. A structural term (a compound relation) is already logic, not
    a source's wording, so it is passed through untouched.
    """
    if isinstance(relation, StringLit):
        canonical = relation_canonical_variant(relation.value, aliases)
        return relation if canonical == relation.value else StringLit(canonical)
    if isinstance(relation, Atom):
        canonical = relation_canonical_variant(relation.name, aliases)
        if canonical == relation.name:
            return relation
        try:
            return Atom(canonical)
        except ValueError:
            # A canonical label that is not a valid atom name (spaces, Hangul,
            # …) cannot stay an Atom; keep it a term of the same textual value.
            return StringLit(canonical)
    return relation
