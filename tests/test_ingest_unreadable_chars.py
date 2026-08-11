# SPDX-License-Identifier: MPL-2.0
"""#473: a NUL the extractor could not map must not enter the KB unremarked.

pypdf emits `\\x00` for a glyph whose font has no ToUnicode map. Those NULs
reached the artifact file and `source_chunks.text` untouched. Here we pin the
replacement, the count, and the three places the count has to surface.

All input is synthetic: a `.nulx` converter registered per test, never a real
PDF and never a real filename.
"""

import hashlib
import sqlite3
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


def _artifact_count(store: Store, artifact_id: int):
    """The stored `unreadable_chars`, read straight off the row.

    Read from the DB rather than the returned dict on purpose: sanitizing the
    text correctly and never persisting the count is exactly the half-done
    state the display layer would then be unable to report.
    """
    return store.get_source_artifact(artifact_id)["unreadable_chars"]


def test_ingest_persists_the_count_on_the_artifact_row(tmp_path, nulx):
    store = _store(tmp_path)
    dirty = tmp_path / "dirty.nulx"
    dirty.write_bytes(_DIRTY.encode("utf-8"))
    clean = tmp_path / "clean.nulx"
    clean.write_bytes("all of this is readable".encode("utf-8"))

    dirty_result = ingest_file(store, dirty, root=tmp_path)
    clean_result = ingest_file(store, clean, root=tmp_path)

    assert _artifact_count(store, int(dirty_result["artifact_id"])) == 7
    # 0, not NULL: this extraction was measured and lost nothing.
    assert _artifact_count(store, int(clean_result["artifact_id"])) == 0
    store.close()


def test_an_artifact_registered_without_a_count_stays_unmeasured(tmp_path):
    """A caller that does not measure must not be recorded as having found zero."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/legacy.txt", kind="text")

    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path="artifacts/sources/1/abc.txt",
        checksum="abc",
    )

    assert _artifact_count(store, artifact_id) is None
    store.close()


def test_re_registering_without_a_count_keeps_the_one_already_recorded(tmp_path):
    """Re-ingesting identical text must not erase a count with a missing one.

    Same (source, kind, checksum) is the conflict the insert already tolerated;
    the count now rides along, so the clause has to prefer a real number on
    either side over the None a non-measuring caller passes.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/doc.txt", kind="text")
    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path="artifacts/sources/1/abc.txt",
        checksum="abc",
        unreadable_chars=7,
    )

    again = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path="artifacts/sources/1/abc.txt",
        checksum="abc",
    )

    assert again == artifact_id
    assert _artifact_count(store, artifact_id) == 7
    store.close()


def test_a_kb_written_before_the_column_existed_gains_it_on_open(tmp_path):
    """The only guard on the migration: schema.sql cannot fix an existing table.

    `init_schema()` runs schema.sql before `_ensure_schema_migrations()`, and
    that script's `CREATE TABLE IF NOT EXISTS` does nothing to a table that is
    already there. So deleting the column from the canonical DDL would leave
    this passing, while deleting the ALTER breaks every KB that predates #473.

    Column existence alone is not enough to assert -- a column nothing can read
    or write is not a migration -- so this also round-trips a value through it.
    """
    db_path = tmp_path / "kb.sqlite"
    store = Store(db_path)
    store.init_schema()
    source_id = store.add_source("sources/old.txt", kind="text")
    legacy_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path="artifacts/sources/1/old.txt",
        checksum="old",
        unreadable_chars=4,
    )
    store.close()

    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE source_artifacts DROP COLUMN unreadable_chars")
    conn.commit()
    conn.close()

    reopened = Store(db_path)
    reopened.init_schema()

    columns = {
        row["name"]
        for row in reopened._conn.execute("PRAGMA table_info(source_artifacts)")
    }
    assert "unreadable_chars" in columns
    # The pre-existing row comes back unmeasured, not zero: the count it once
    # had went with the column, and inventing one here would be a lie.
    assert _artifact_count(reopened, legacy_id) is None

    fresh_id = reopened.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path="artifacts/sources/1/new.txt",
        checksum="new",
        unreadable_chars=3,
    )
    assert _artifact_count(reopened, fresh_id) == 3
    reopened.close()


def _sources_client(tmp_path):
    """A local TestClient, deliberately not `tests/test_web.py`'s shared `_client`.

    Local so this file's rows cannot collide with another lane's fixtures, and a
    real client rather than a hand-built dict because `web/app.py:1425` is what
    turns each artifact row into the mapping the template indexes -- rendering a
    dict made here would skip exactly that step.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web import create_app

    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    return TestClient(create_app(cfg))


def test_sources_page_separates_measured_loss_from_never_measured(tmp_path, nulx):
    """Three rows, one GET: a lossy artifact, a clean one, and a legacy NULL one."""
    client = _sources_client(tmp_path)
    store = client.app.state.store

    dirty = tmp_path / "dirty.nulx"
    dirty.write_bytes(_DIRTY.encode("utf-8"))
    ingest_file(store, dirty, root=tmp_path)

    clean = tmp_path / "clean.nulx"
    clean.write_bytes("every character here was readable".encode("utf-8"))
    ingest_file(store, clean, root=tmp_path)

    # A row as an older verinote wrote it: registered without a count.
    legacy_id = store.add_source("sources/legacy.txt", kind="text")
    store.add_source_artifact(
        source_id=legacy_id,
        kind="extracted_text",
        path=f"artifacts/sources/{legacy_id}/legacy.txt",
        checksum="legacy",
    )

    html = client.get("/sources").text

    # 7 rather than 1: it collides with no other number on the page, so
    # rendering the artifact count, the source id or a literal all fail here.
    assert "7 unreadable character(s)" in html
    assert "unreadable characters not checked" in html
    assert html.count("unreadable character(s)") == 1
    assert html.count("unreadable characters not checked") == 1
