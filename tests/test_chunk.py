# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.pipeline.chunk import chunk_text


def test_chunk_text_returns_empty_for_blank_source():
    assert chunk_text(" \n\n ") == []


def test_chunk_text_keeps_small_source_as_one_chunk():
    assert chunk_text("alpha\n\nbeta", max_chars=100) == ["alpha\n\nbeta"]


def test_chunk_text_splits_on_paragraph_boundaries_with_overlap():
    chunks = chunk_text("alpha one\n\nbeta two\n\ngamma three", max_chars=15, overlap_chars=5)

    assert len(chunks) == 3
    assert chunks[0] == "alpha one"
    assert chunks[1].startswith("a one\n\nbeta two")
    assert chunks[2].startswith("a two\n\ngamma three")


def test_chunk_text_splits_long_paragraphs():
    chunks = chunk_text("abcdefghij", max_chars=4, overlap_chars=0)

    assert chunks == ["abcd", "efgh", "ij"]


def test_chunk_text_rejects_non_positive_max_chars():
    with pytest.raises(ValueError):
        chunk_text("body", max_chars=0)
