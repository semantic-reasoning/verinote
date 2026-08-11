# SPDX-License-Identifier: MPL-2.0
"""#473: a NUL the extractor could not map must not enter the KB unremarked.

pypdf emits `\\x00` for a glyph whose font has no ToUnicode map. Those NULs
reached the artifact file and `source_chunks.text` untouched. Here we pin the
replacement, the count, and the three places the count has to surface.

All input is synthetic: a `.nulx` converter registered per test, never a real
PDF and never a real filename.
"""

import hashlib
import unicodedata

import pytest

from verinote.pipeline.extract import create_chunked_extraction_job
from verinote.pipeline.ingest import (
    _CONVERTERS,
    ingest_file,
    register_converter,
    sanitize_extracted_text,
    store_source,
)
from verinote.store import Store


@pytest.fixture
def nulx():
    """A converter for a made-up binary extension that passes bytes through.

    Stands in for pypdf without needing a PDF: the point under test is what
    ingest does with whatever the converter hands back, and a real document
    would put real data in the repository.
    """
    register_converter(".nulx", lambda raw: raw.decode("utf-8"))
    yield
    _CONVERTERS.pop(".nulx", None)


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


@pytest.mark.parametrize(
    "raw, expected_text, expected_count",
    [
        # Nothing to do: text without NUL comes back identical and counts zero.
        ("clean text", "clean text", 0),
        ("a\x00b", "a�b", 1),
        # Runs and edges, so a count that only looks at the interior fails.
        ("\x00a\x00b\x00c\x00d\x00", "�a�b�c�d�", 5),
        # The shape from the bug report: `F-13` arrived as `F\x0013`. Deleting
        # the NUL would silently manufacture the fact `F13`.
        ("F\x0013", "F�13", 1),
        # Non-ASCII neighbours: the replacement is per character, not per byte.
        ("가\x00나", "가�나", 1),
        # A replacement character the input already carried is not this
        # extraction's loss: two NULs in, three U+FFFD out, count is 2.
        ("�\x00\x00", "���", 2),
        # Only NUL. `\x0c` is pdftotext's page separator and the rest are
        # ordinary whitespace -- widening to Cc would eat all four.
        ("\t\n\r\x0c", "\t\n\r\x0c", 0),
    ],
)
def test_sanitize_replaces_nul_and_counts_the_input(raw, expected_text, expected_count):
    result = sanitize_extracted_text(raw)

    assert result.text == expected_text
    assert result.unreadable_chars == expected_count


def test_sanitize_leaves_decomposed_text_decomposed():
    """Sanitizing is not normalizing: NFD in, NFD out, only the NUL changes.

    `store_source` applies `nfc()` first and then sanitizes; if this function
    also normalized, the order of those two would stop mattering and a later
    reordering would go unnoticed.
    """
    decomposed = unicodedata.normalize("NFD", "가나")
    assert decomposed != "가나"

    result = sanitize_extracted_text(decomposed[:2] + "\x00" + decomposed[2:])

    assert result.text == decomposed[:2] + "�" + decomposed[2:]
    assert unicodedata.normalize("NFC", result.text) == "가�나"
    assert result.unreadable_chars == 1


# Seven NULs, shaped like the report: a hyphen, a tilde and a run of glyphs the
# extractor could not map.
_DIRTY = "F\x0013\nF\x0018\n07/27 \x00 08/02\n가\x00나\x00다\x00라\x00마"
_CLEAN = "F�13\nF�18\n07/27 � 08/02\n가�나�다�라�마"


def test_every_sink_of_one_ingest_sees_the_same_sanitized_text(tmp_path, nulx):
    """Artifact file, chunk rows, checksum and returned count, in one ingest.

    Sanitizing in only some of `store_source`'s sinks is the failure this
    catches: the digest naming the artifact would stop matching the bytes in
    it, or the chunks fed to a provider would still carry NUL while the file on
    disk looked clean.

    The job call mirrors `verinote/web/app.py:1937-1946` -- same function, same
    argument order, `source_text=result["text"]` -- so the chunk assertion is
    about the path the uploader actually takes.
    """
    store = _store(tmp_path)
    src = tmp_path / "plan.nulx"
    src.write_bytes(_DIRTY.encode("utf-8"))

    result = ingest_file(store, src, root=tmp_path)
    job_id = create_chunked_extraction_job(
        store,
        source_id=int(result["source_id"]),
        artifact_id=int(result["artifact_id"]),
        source_text=result["text"],
        provider="anthropic",
        model="m",
        chunk_chars=None,
        chunk_overlap_chars=None,
    )

    artifact_text = (tmp_path / result["artifact_path"]).read_text(encoding="utf-8")
    assert "\x00" not in artifact_text
    assert artifact_text == _CLEAN

    chunks = store.source_chunks(job_id)
    assert chunks, "the job must have produced chunks for this assertion to mean anything"
    assert all("\x00" not in chunk["text"] for chunk in chunks)

    digest = hashlib.sha256(_CLEAN.encode("utf-8")).hexdigest()
    artifact = store.get_source_artifact(int(result["artifact_id"]))
    assert artifact["checksum"] == digest
    assert result["artifact_path"].endswith(f"{digest}.txt")

    assert result["unreadable_chars"] == 7
    store.close()


def test_a_clean_ingest_reports_zero_not_none(tmp_path, nulx):
    """0 and None are different answers; an ingest that ran always knows a number."""
    store = _store(tmp_path)
    src = tmp_path / "clean.nulx"
    src.write_bytes("nothing was lost here".encode("utf-8"))

    result = ingest_file(store, src, root=tmp_path)

    assert result["unreadable_chars"] == 0
    assert result["text"] == "nothing was lost here"
    store.close()


def test_store_source_sanitizes_text_uploads_too(tmp_path):
    """The single decision point is in `store_source`, not in the pdf converter.

    A `.txt` upload never touches a converter, so if the replacement lived in
    `_convert_pdf` this text would reach the artifact with its NUL intact.
    """
    store = _store(tmp_path)

    result = store_source(store, tmp_path, "notes.txt", b"a\x00b", "a\x00b", "text")

    assert result["text"] == "a�b"
    assert result["unreadable_chars"] == 1
    assert (tmp_path / result["artifact_path"]).read_text(encoding="utf-8") == "a�b"
    # The original upload is kept byte-for-byte: sanitizing is about the
    # extraction, and the file a fact cites must stay what was uploaded.
    assert (tmp_path / "sources" / "notes.txt").read_bytes() == b"a\x00b"
    store.close()
