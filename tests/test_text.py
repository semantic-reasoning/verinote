# SPDX-License-Identifier: MPL-2.0
"""The one Unicode normalizer, and the term-aware wrapper that feeds it leaves."""

import unicodedata

from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var
from verinote.store.fact_input import nfc_term
from verinote.text import nfc

NFD_CAFE = unicodedata.normalize("NFD", "café")
NFC_CAFE = unicodedata.normalize("NFC", "café")


def test_nfd_input_normalizes_to_nfc():
    # The fixture proves nothing unless the two forms differ in bytes first.
    assert NFD_CAFE != NFC_CAFE
    assert nfc(NFD_CAFE) == NFC_CAFE


def test_nfc_is_idempotent():
    once = nfc(NFD_CAFE)
    assert nfc(once) == once


def test_ascii_passes_through_unchanged():
    assert nfc("plain ascii 123") == "plain ascii 123"


def test_nfc_term_normalizes_a_string_leaf():
    assert nfc_term(StringLit(NFD_CAFE)) == StringLit(NFC_CAFE)


def test_nfc_term_recurses_into_compound_args_normalizing_only_string_leaves():
    term = Compound("note", (StringLit(NFD_CAFE), NumberLit(7), Atom("plain")))
    assert nfc_term(term) == Compound(
        "note", (StringLit(NFC_CAFE), NumberLit(7), Atom("plain"))
    )


def test_nfc_term_recurses_into_nested_compounds():
    term = Compound("outer", (Compound("inner", (StringLit(NFD_CAFE),)),))
    assert nfc_term(term) == Compound(
        "outer", (Compound("inner", (StringLit(NFC_CAFE),)),)
    )


def test_nfc_term_leaves_non_string_term_kinds_untouched():
    # Atoms, functors, numbers, and variables are ASCII-only by construction, so
    # StringLit is the only leaf that can hold non-NFC text. The others return
    # the very same object, unchanged.
    for term in (NumberLit(5), Atom("plain"), Var("X")):
        assert nfc_term(term) is term
