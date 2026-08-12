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
import threading
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
from verinote.text import nfc


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
        # The shape from the bug report, with a placeholder identifier: a hyphen
        # between two runs of digits arrives as `A\x0001`. Deleting the NUL would
        # silently manufacture the fact `A01`.
        ("A\x0001", "A�01", 1),
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


# Seven NULs in the shape the report describes -- a hyphen, a tilde and a run
# of glyphs the extractor could not map -- over placeholder content. The shape
# is what the assertions turn on, so nothing is lost by not being the document.
_DIRTY = "A\x0001\nA\x0002\n01/01 \x00 01/07\n가\x00나\x00다\x00라\x00마"
_CLEAN = "A�01\nA�02\n01/01 � 01/07\n가�나�다�라�마"


def test_every_sink_of_one_ingest_sees_the_same_sanitized_text(tmp_path, nulx):
    """Artifact file, chunk rows, checksum and returned count, in one ingest.

    Sanitizing in only some of `store_source`'s sinks is the failure this
    catches: the digest naming the artifact would stop matching the bytes in
    it, or the chunks fed to a provider would still carry NUL while the file on
    disk looked clean.

    The job call mirrors `verinote/web/app.py:1936-1945` -- same function, same
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

    # Every NUL-absence check below is paired with the exact expected string,
    # and with the length: replacement preserves it, deletion would lose seven.
    # NUL-absence on its own passes for `replace("\x00", "")` -- the silent
    # removal the issue rejected as option (a).
    artifact_text = (tmp_path / result["artifact_path"]).read_text(encoding="utf-8")
    assert "\x00" not in artifact_text
    assert artifact_text == _CLEAN
    assert len(artifact_text) == len(_DIRTY)

    chunks = store.source_chunks(job_id)
    assert [chunk["text"] for chunk in chunks] == [_CLEAN], (
        "the fixture is far shorter than one chunk, so the chunk table is the "
        "sanitized text exactly once -- NUL-free is not enough to assert here"
    )

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


def _artifact_key(source_id: int) -> dict:
    """One (source_id, kind, checksum) — the conflict target of the insert."""
    return {
        "source_id": source_id,
        "kind": "extracted_text",
        "path": f"artifacts/sources/{source_id}/abc.txt",
        "checksum": "abc",
    }


def test_re_registering_with_a_count_fills_in_one_never_measured(tmp_path):
    """An unmeasured row gains its count when something re-ingests and measures.

    This is the direction `DO UPDATE` adds, and the reason the clause changed.
    The opposite direction -- a missing count leaving an existing one alone --
    is a property the old `DO NOTHING` had for free, so a suite that asserted
    only that would stay green with the clause reverted.
    """
    store = _store(tmp_path)
    key = _artifact_key(store.add_source("sources/doc.txt", kind="text"))
    artifact_id = store.add_source_artifact(**key)
    assert _artifact_count(store, artifact_id) is None

    again = store.add_source_artifact(**key, unreadable_chars=7)

    assert again == artifact_id
    assert _artifact_count(store, artifact_id) == 7
    store.close()


def test_re_registering_without_a_count_keeps_the_one_already_recorded(tmp_path):
    """Re-ingesting identical text must not erase a count with a missing one.

    Same (source, kind, checksum) is the conflict the insert already tolerated;
    the count now rides along, so the clause has to prefer a real number on
    either side over the None a non-measuring caller passes.
    """
    store = _store(tmp_path)
    key = _artifact_key(store.add_source("sources/doc.txt", kind="text"))
    artifact_id = store.add_source_artifact(**key, unreadable_chars=7)

    again = store.add_source_artifact(**key)

    assert again == artifact_id
    assert _artifact_count(store, artifact_id) == 7
    store.close()


def _legacy_text_artifact(store: Store, root, citation: str, text: str) -> str:
    """A row as a pre-#473 verinote left it: no count, digest of raw text.

    The digest is the load-bearing part. Back then nothing sanitized, so the
    checksum names the text the extractor produced -- NUL and all -- which is
    what decides whether a later measuring ingest conflicts with this row or
    sits down beside it. Returns the checksum.
    """
    source_id = store.add_source(citation, kind="binary")
    digest = hashlib.sha256(nfc(text).encode("utf-8")).hexdigest()
    relpath = f"artifacts/sources/{source_id}/{digest}.txt"
    artifact_file = root / relpath
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.write_text(text, encoding="utf-8")
    store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path=relpath,
        checksum=digest,
    )
    return digest


def test_a_pre_column_row_that_held_nul_gains_a_second_row_not_a_backfill(
    tmp_path, nulx
):
    """The narrow precondition on that backfill, made executable.

    Re-registering fills in a missing count only for the SAME checksum, and a
    pre-column row was hashed from unsanitized text. Where that text held NUL,
    sanitizing moves the digest, so the measuring re-ingest never reaches the
    conflict target: it INSERTs a second row with the count and the first keeps
    its NULL for good.

    Documentation that runs, not a mutation guard, and it does not claim to be
    one: the change it accompanies is prose, and this stays green under
    `DO UPDATE` -> `DO NOTHING` because nothing here ever conflicts.
    `test_re_registering_with_a_count_fills_in_one_never_measured` owns that
    kill, and `test_every_sink_of_one_ingest_sees_the_same_sanitized_text` owns
    the one for hashing before sanitizing. Its worth is that the claim in
    `Store.add_source_artifact`'s docstring is now checked by something that
    executes, instead of by a reader.
    """
    store = _store(tmp_path)
    legacy_digest = _legacy_text_artifact(store, tmp_path, "sources/plan.nulx", _DIRTY)
    src = tmp_path / "plan.nulx"
    src.write_bytes(_DIRTY.encode("utf-8"))

    result = ingest_file(store, src, root=tmp_path)

    # Same source: it is the one path, re-ingested, so this is the row the
    # count would have to land on for a backfill to be possible at all.
    source_id = int(result["source_id"])
    rows = store.source_artifacts(source_id)
    assert len(rows) == 2
    assert [row["unreadable_chars"] for row in rows] == [None, 7]
    # The mechanism, spelled out: the digests differ because one is taken over
    # the NULs and the other over their replacements.
    clean_digest = hashlib.sha256(_CLEAN.encode("utf-8")).hexdigest()
    assert legacy_digest != clean_digest
    assert [row["checksum"] for row in rows] == [legacy_digest, clean_digest]
    assert int(result["artifact_id"]) == int(rows[1]["id"])
    store.close()


def test_a_pre_column_row_that_held_no_nul_is_backfilled_by_a_re_ingest(tmp_path, nulx):
    """The other side of that precondition: nothing to sanitize, digest holds.

    An unmeasured row over text with no NUL is the case the backfill does
    reach, and the count it gains is 0 -- the only count such a row could ever
    have had. Same standing as the test above: it pins the docstring's claim,
    not a clause, and shares its kills with the re-registration tests.
    """
    store = _store(tmp_path)
    readable = "every character here was readable"
    legacy_digest = _legacy_text_artifact(
        store, tmp_path, "sources/plan.nulx", readable
    )
    src = tmp_path / "plan.nulx"
    src.write_bytes(readable.encode("utf-8"))

    result = ingest_file(store, src, root=tmp_path)

    rows = store.source_artifacts(int(result["source_id"]))
    assert len(rows) == 1, "the digest is unchanged, so this is an update"
    assert rows[0]["checksum"] == legacy_digest
    # 0 rather than None: the re-ingest measured, and found nothing lost.
    assert rows[0]["unreadable_chars"] == 0
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
    assert "not checked for unreadable characters" in html
    # The class is the signal -- colour is how this row grades severity -- so it
    # is worth coupling to. Bare `.warn-inline`, like the "N pending" span
    # further along the row: with `.badge` added it would be a chip, the same
    # shape as the `extracted_text` badge beside it, differing only in hue.
    assert '<span class="warn-inline">7 unreadable character(s)</span>' in html
    # Neither phrase is a substring of the other -- "unreadable character(s)"
    # carries the parenthesised plural, the note does not -- so these two counts
    # stay independent.
    assert html.count("unreadable character(s)") == 1
    assert html.count("not checked for unreadable characters") == 1


_UNMEASURED_NOTE = (
    '<span class="badge src">not checked for unreadable characters — some may '
    "remain; re-upload to fix, or delete the source and upload it again if "
    "this note stays</span>"
)

# The wording this replaced, which told every unmeasured row to delete its
# source -- destructive, and unnecessary for the majority whose extraction held
# no NUL. `"; re-upload to fix"` on its own is no longer a regression guard: the
# new note opens with that clause. The closing tag is what pins the old one.
_DESTRUCTIVE_FIRST_NOTE = "; delete the source and upload it again to fix</span>"
_RE_UPLOAD_ONLY_NOTE = "; re-upload to fix</span>"


def _block_the_extraction_worker(monkeypatch) -> list[int]:
    """Let the upload route queue its job, and let no worker touch the KB.

    `ExtractionJobBusyError` is the one outcome the worker answers by writing
    nothing at all -- every other exit runs `fail_extraction_job` -- so raising
    it keeps a background thread from racing the assertions below over the same
    sqlite file. `get_client` is patched too because it runs FIRST: left real,
    it would raise on this key-less config and reach the generic handler, which
    does write. Returns the list of job ids the worker was asked for.
    """
    from verinote.pipeline import ExtractionJobBusyError
    from verinote.web import app as webapp

    asked: list[int] = []

    def refuse(_store, _client, *, job_id, **_kwargs):
        asked.append(job_id)
        raise ExtractionJobBusyError(f"job {job_id} is not analysed in this test")

    monkeypatch.setattr(webapp, "get_client", lambda _cfg: object())
    monkeypatch.setattr(webapp, "process_extraction_job", refuse)
    return asked


def _join_extraction_workers() -> None:
    """Wait out the threads the upload route starts, by their given name.

    The thread is started synchronously inside the POST, so by the time the
    response is in hand it is already enumerable -- no window to miss one.
    Joining rather than sleeping is what makes the row assertions deterministic.
    """
    for thread in threading.enumerate():
        if thread.name.startswith("verinote-source-extract-"):
            thread.join(timeout=5.0)
            assert not thread.is_alive(), f"{thread.name} outlived its join"


def test_the_sources_page_names_an_action_that_actually_clears_the_unmeasured_note(
    tmp_path, monkeypatch, nulx
):
    """This source is the note's harder population, and both of its clauses run.

    The note offers re-upload first and deleting the source only "if this note
    stays". For an artifact whose unsanitized text held NUL, staying is exactly
    what it does: the sanitized upload hashes differently, INSERTs a measured
    row rather than filling this one in, and the page renders both. So leg 2
    performs the first clause and shows it is not enough here, which is what
    makes the second clause reachable rather than gratuitous; leg 3 performs
    the second and shows the note gone. Rendering assertions alone would not
    separate those -- any wording renders.

    `test_a_re_upload_clears_the_note_when_the_old_extraction_held_no_nul`
    carries the other population, where the first clause is the whole answer.
    """
    asked = _block_the_extraction_worker(monkeypatch)
    client = _sources_client(tmp_path)
    store = client.app.state.store

    # A source as a pre-#473 verinote left it: the original file, an artifact
    # hashed over text that still holds its NULs, and no count.
    upload = {"file": ("plan.nulx", _DIRTY.encode("utf-8"), "application/octet-stream")}
    source_dir = tmp_path / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "plan.nulx").write_bytes(_DIRTY.encode("utf-8"))
    legacy_digest = _legacy_text_artifact(store, tmp_path, "sources/plan.nulx", _DIRTY)
    source_id = int(store.get_source_by_path("sources/plan.nulx")["id"])
    legacy_file = tmp_path / f"artifacts/sources/{source_id}/{legacy_digest}.txt"

    # Leg 1: the wording, whole, as the browser receives it. The `.badge src`
    # class and the em dash are inside the assertion because the reviewer's
    # earlier assertions couple to them; a substring of the sentence would let
    # the chip quietly become plain text.
    html = client.get("/sources").text
    assert _UNMEASURED_NOTE in html
    # Both discarded wordings, pinned by their closing tag. A bare
    # `"; re-upload to fix"` would match the current note's first clause and
    # guard nothing; ending at `</span>` is what makes each of these the whole
    # of a note rather than a prefix of one.
    assert _RE_UPLOAD_ONLY_NOTE not in html
    assert _DESTRUCTIVE_FIRST_NOTE not in html
    assert html.count("not checked for unreadable characters") == 1

    # Leg 2: do what the note says first -- upload the same bytes again -- and
    # watch it fail to clear anything for THIS population.
    response = client.post("/sources", files=upload, follow_redirects=False)
    assert response.status_code == 303
    _join_extraction_workers()

    rows = store.source_artifacts(source_id)
    assert [row["unreadable_chars"] for row in rows] == [None, 7], (
        "the re-upload sanitized, so it hashed differently and INSERTed; the "
        "unmeasured row is untouched"
    )
    html = client.get("/sources").text
    assert html.count("not checked for unreadable characters") == 1, (
        "the note stayed, which is the condition its second clause names"
    )
    assert "7 unreadable character(s)" in html, (
        "both rows render: this is the side-by-side state the comment warns is "
        "a sharp edge, the stale note above a correct count"
    )

    # Leg 3: the note stayed, so follow its second clause. Delete takes the row
    # and its file, and the upload after it leaves one measured artifact.
    # Asserted before as well as after: `not exists()` is true of a path that
    # was never right, and would pass this leg without a delete happening.
    assert legacy_file.exists()
    response = client.post(f"/sources/{source_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert not legacy_file.exists()
    response = client.post("/sources", files=upload, follow_redirects=False)
    assert response.status_code == 303
    _join_extraction_workers()

    html = client.get("/sources").text
    assert "not checked for unreadable characters" not in html
    assert "7 unreadable character(s)" in html
    # Both uploads queued a job and neither was analysed, so nothing above came
    # from a worker write -- and the stub really did stand in the way.
    assert len(asked) == 2


def test_a_re_upload_clears_the_note_when_the_old_extraction_held_no_nul(
    tmp_path, monkeypatch, nulx
):
    """The note's first clause, on the page, for the population it is meant for.

    Most documents contain no NUL, so most unmeasured rows hash the same before
    and after sanitizing: the re-upload the note asks for first backfills the
    row with 0 and the note disappears. That is what makes offering the
    destructive action second, and only conditionally, honest -- these readers
    never reach it.

    `test_a_pre_column_row_that_held_no_nul_is_backfilled_by_a_re_ingest` shows
    the same backfill one layer down. This one is at the layer the wording
    makes a promise about: the reader sees a note, does what it says, and the
    note is gone.
    """
    _block_the_extraction_worker(monkeypatch)
    client = _sources_client(tmp_path)
    store = client.app.state.store

    readable = "every character here was readable"
    source_dir = tmp_path / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "plan.nulx").write_bytes(readable.encode("utf-8"))
    _legacy_text_artifact(store, tmp_path, "sources/plan.nulx", readable)
    source_id = int(store.get_source_by_path("sources/plan.nulx")["id"])

    # Asserted before as well as after: "the note is gone" is also true of a
    # page that never rendered one, which would pass this while proving nothing.
    assert _UNMEASURED_NOTE in client.get("/sources").text

    response = client.post(
        "/sources",
        files={
            "file": ("plan.nulx", readable.encode("utf-8"), "application/octet-stream")
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    _join_extraction_workers()

    # One row, not two: the digest was reproducible, so the upload updated this
    # artifact instead of sitting a measured sibling next to it.
    rows = store.source_artifacts(source_id)
    assert [row["unreadable_chars"] for row in rows] == [0]
    html = client.get("/sources").text
    assert "not checked for unreadable characters" not in html
    # And nothing took its place: 0 is measured and lossless, so this row now
    # reports neither state. A note swapped for a count would not be a fix.
    assert "unreadable character(s)" not in html


def _cli_env(monkeypatch, tmp_path):
    """Point `cli.main` at a fresh KB under tmp_path, as tests/test_cli.py does."""
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


def test_cli_ingest_warns_how_many_characters_it_could_not_read(
    tmp_path, monkeypatch, capsys, nulx
):
    from verinote import cli

    _cli_env(monkeypatch, tmp_path)
    src = tmp_path / "lossy.nulx"
    src.write_bytes("a\x00b\x00c\x00d".encode("utf-8"))

    rc = cli.main(["ingest", str(src)])

    captured = capsys.readouterr()
    assert rc == 0
    # Anchored on both sides of the number. A bare `"3" in err` passes for any
    # count, or none, because the warning interpolates `args.path` and a pytest
    # tmp path supplies stray digits; and without the leading `warning: ` the
    # phrase is still a suffix of a wrong count like "103 character(s)".
    assert "warning: 3 character(s) could not be read" in captured.err
    # The warning is an aside; the citation line stdout already promised stays.
    assert "ingested" in captured.out and "-> sources/" in captured.out


def test_cli_ingest_says_nothing_when_every_character_was_readable(
    tmp_path, monkeypatch, capsys, nulx
):
    """The other half: a warning printed unconditionally would be no warning."""
    from verinote import cli

    _cli_env(monkeypatch, tmp_path)
    src = tmp_path / "fine.nulx"
    src.write_bytes("abcd".encode("utf-8"))

    rc = cli.main(["ingest", str(src)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "could not be read" not in captured.err
    assert "ingested" in captured.out and "-> sources/" in captured.out
