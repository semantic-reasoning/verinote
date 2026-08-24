# SPDX-License-Identifier: MPL-2.0
"""#494: NULs already stored before #473's sanitizer must be findable.

#473 stopped new NULs entering the KB. It reached nothing already written, so a
KB ingested before it still holds them in its extraction-text artifact files and
in `source_chunks.text`. The scan here reports them and repairs nothing.

The population that matters is the *unmigrated* KB — one whose
`source_artifacts` table has no `unreadable_chars` column, because that column
arrived with #473. `_unmigrated_kb` builds that; `_legacy_source` builds the
easier state, a migrated KB still carrying pre-#473 rows and files.
"""

import sqlite3

import pytest

import verinote.cli as cli
from verinote.pipeline.extract import create_chunked_extraction_job
from verinote.pipeline.ingest import store_source
from verinote.pipeline.policy_state import POLICY_RELPATH
from verinote.store import Store

# #473's reported shape, where deleting the NUL rather than replacing it would
# manufacture the identifier `A01`.
_DIRTY = "A\x0001 and \x00"
_DIRTY_NULS = 2


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _write_artifact(root, source_id: int, name: str, text: str) -> str:
    rel = f"artifacts/sources/{source_id}/{name}.txt"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _legacy_source(store, root, *, name, text, kind="extracted_text"):
    """A migrated KB whose artifact file and chunks still hold their NULs.

    `store_source` is not used on purpose: it sanitizes, which is the whole
    thing this data predates. The artifact file is written directly and the
    chunks come from `create_chunked_extraction_job`, the real pre-#473
    re-analysis path — it chunks whatever text it is handed and sanitizes
    nothing (`normalize_for_extraction` does not touch NULs).

    This is NOT what a KB written before #473 looks like on disk: that KB has no
    `unreadable_chars` column at all. `_unmigrated_kb` is that one.
    """
    source_id = store.add_source(f"sources/{name}", kind="binary")
    rel = _write_artifact(root, source_id, name, text)
    artifact_id = store.add_source_artifact(
        source_id=source_id, kind=kind, path=rel, checksum=name,
    )  # unreadable_chars omitted -> NULL, the "never measured" state
    job_id = create_chunked_extraction_job(
        store, source_id=source_id, artifact_id=artifact_id,
        source_text=text, provider=None, model=None,
    )
    # Preconditions, not decoration: if anything ever starts sanitizing on this
    # path the fixture stops planting what the tests claim to find.
    assert (root / rel).read_text(encoding="utf-8").count("\x00") == text.count("\x00")
    assert sum(r["text"].count("\x00") for r in store.source_chunks(job_id)) > 0
    return source_id, artifact_id, job_id


def _repaired_source(store, root, *, name):
    """`_legacy_source`, then the clean rows a re-upload and a re-sync leave.

    The post-remediation shape: a dirty superseded artifact under a clean newer
    one, and a superseded job's dirty chunks under a clean newer job. Both are
    reachable — a re-upload INSERTs a second artifact rather than filling the
    first one in, and `verinote sync` builds a fresh job without clearing the
    old one's chunks.
    """
    source_id, old_artifact_id, old_job_id = _legacy_source(
        store, root, name=name, text=_DIRTY
    )
    clean = _DIRTY.replace("\x00", "�")
    rel = _write_artifact(root, source_id, f"{name}-clean", clean)
    new_artifact_id = store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel,
        checksum=f"{name}-clean", unreadable_chars=_DIRTY_NULS,
    )
    new_job_id = create_chunked_extraction_job(
        store, source_id=source_id, artifact_id=new_artifact_id,
        source_text=clean, provider=None, model=None,
    )
    assert new_artifact_id > old_artifact_id and new_job_id > old_job_id
    return source_id, old_artifact_id, new_artifact_id, old_job_id, new_job_id


_UNMIGRATED_SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    relation TEXT NOT NULL,
    object TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE review_log (id INTEGER PRIMARY KEY);
CREATE TABLE runs (id INTEGER PRIMARY KEY);
CREATE TABLE source_artifacts (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    checksum TEXT NOT NULL DEFAULT ''
);
"""


def _unmigrated_kb(tmp_path, *, text=_DIRTY):
    """A KB as it stood before #473: no `unreadable_chars`, no `source_chunks`.

    Hand-rolled the way `tests/test_cli.py`'s legacy-KB test hand-rolls its own,
    and for the same reason: every fixture built through `init_schema()` is a
    *migrated* KB, so none of them can stand in for this one. The four tables
    above are what `_KB_CORE_TABLES` demands, so this file passes
    `_require_existing_kb` — that is what makes it a reachable state and not a
    straw fixture, and it is asserted in the test that uses it.

    `source_artifacts` here has no `unreadable_chars` column and there is no
    `source_chunks` table, because both arrived by migration.
    """
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.executescript(_UNMIGRATED_SCHEMA)
    conn.execute("INSERT INTO sources(path, kind) VALUES('sources/legacy.pdf', 'binary')")
    source_id = int(conn.execute("SELECT id FROM sources").fetchone()[0])
    rel = _write_artifact(tmp_path, source_id, "legacy", text)
    conn.execute(
        "INSERT INTO source_artifacts(source_id, kind, path, checksum) "
        "VALUES(?, 'extracted_text', ?, 'legacy')",
        (source_id, rel),
    )
    conn.commit()
    conn.close()
    return source_id, rel


def _only_source(scan):
    assert len(scan.sources) == 1
    return scan.sources[0]


def _source_line(out: str, path: str) -> str:
    """The one report line for `path`.

    Assertions about a source's verdict have to be made against its own line:
    the summary line names every category, so `"in superseded rows only" not in
    out` is satisfied by nothing and fails on the summary's own "0 in
    superseded rows only".
    """
    lines = [line for line in out.splitlines() if line.startswith(f"{path}: ")]
    assert len(lines) == 1, f"expected one report line for {path}, got {lines}"
    return lines[0]


# --- what the scan finds ---------------------------------------------------


def test_scan_finds_nuls_in_artifact_file(tmp_path):
    store = _store(tmp_path)
    _legacy_source(store, tmp_path, name="legacy", text=_DIRTY)

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert len(source.artifacts) == 1
    artifact = source.artifacts[0]
    assert artifact.status == "found"
    assert artifact.nuls == _DIRTY_NULS
    assert artifact.recorded is None
    assert artifact.is_latest is True
    assert scan.artifacts_scanned == 1


def test_scan_finds_nuls_in_chunk_text(tmp_path):
    store = _store(tmp_path)
    _, _, job_id = _legacy_source(store, tmp_path, name="legacy", text=_DIRTY)
    rows = store.source_chunks(job_id)
    planted = {
        int(row["chunk_index"]): row["text"].count("\x00")
        for row in rows
        if row["text"].count("\x00")
    }
    assert planted, "the fixture planted no dirty chunk"

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    # Exactly the planted indices with their exact counts, and no others: a
    # scan that reported every chunk, or one extra, fails here.
    assert {c.chunk_index: c.nuls for c in source.chunks} == planted
    assert source.chunks_scanned == len(rows)
    assert scan.chunks_scanned == len(rows)


def test_scan_counts_every_nul_in_a_chunk(tmp_path):
    """Three NULs, counted as three.

    `length()` truncates at the first NUL and `instr()` returns a position, so
    SQL-side counting reports 0 and 1 respectively for this text. Only a real
    count gives 3.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/three.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, artifact_id=None, provider=None, model=None,
        total_chunks=1, message="",
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=["\x00a\x00b\x00"])

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert [c.nuls for c in source.chunks] == [3]


def test_artifact_and_chunk_counts_are_not_summed(tmp_path, monkeypatch, capsys):
    """One NUL in the artifact becomes two in the chunk store, and neither total
    is the other's business.

    `chunk_text` prepends the previous chunk's last 40 characters to each later
    chunk, so a NUL inside that window is stored twice. This fixture puts one
    there. The report therefore has no headline over both stores — there is no
    number a headline could honestly carry.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _legacy_source(store, tmp_path, name="overlap", text="A" * 290 + "\x00" + "B" * 300)

    scan = store.scan_unreadable_text()
    store.close()
    source = _only_source(scan)

    assert sum(a.nuls for a in source.artifacts) == 1
    assert sum(c.nuls for c in source.chunks) == 2

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    # No aggregate sits between the source and its two segments; each count is
    # printed inside the segment that owns it.
    assert "sources/overlap: unreadable characters still stored — artifacts: " in out
    assert "artifacts: 1 in the latest of 1 artifact row(s)" in out
    assert "chunks: 2 in 2 chunk(s) of the latest job" in out


def test_replacement_char_is_not_a_finding(tmp_path):
    """U+FFFD is what #473 wrote *instead* of a NUL. Counting it would report
    every repaired source as still broken."""
    store = _store(tmp_path)
    source_id = store.add_source("sources/repaired.txt")
    rel = _write_artifact(tmp_path, source_id, "repaired", "a�b")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel, checksum="repaired",
    )
    job_id = store.create_extraction_job(
        source_id=source_id, artifact_id=None, provider=None, model=None,
        total_chunks=1, message="",
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=["a�b"])

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert [(a.status, a.nuls) for a in source.artifacts] == [("clean", 0)]
    assert source.chunks == ()


def test_legacy_original_text_artifact_is_scanned(tmp_path):
    """`original_text` rows are still read by `latest_source_text_artifact` and
    `source_text_inputs`, so narrowing the scan to `extracted_text` would skip
    artifacts re-analysis will happily re-read."""
    store = _store(tmp_path)
    _legacy_source(store, tmp_path, name="orig", text=_DIRTY, kind="original_text")

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert [(a.status, a.nuls) for a in source.artifacts] == [("found", _DIRTY_NULS)]


# --- what the scan could not read is never called clean --------------------


def test_missing_artifact_file_is_not_clean(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/pruned.pdf", kind="binary")
    rel = _write_artifact(tmp_path, source_id, "pruned", _DIRTY)
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel, checksum="pruned",
    )
    (tmp_path / rel).unlink()

    scan = store.scan_unreadable_text()
    store.close()

    assert [(a.status, a.nuls) for a in _only_source(scan).artifacts] == [
        ("file_missing", 0)
    ]
    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "could not scan 1 artifact(s)" in out
    assert f"file_missing: {rel}" in out


def test_undecodable_artifact_file_is_not_clean(tmp_path):
    """Bytes that are not UTF-8 mean "we could not look", not "nothing here".

    The file below holds two NULs a decoding read would have found. Reporting
    it as `found` with a partial count would turn a failed read into a
    measurement; reporting it as `clean` would turn it into an all-clear.
    """
    store = _store(tmp_path)
    source_id = store.add_source("sources/broken.pdf", kind="binary")
    rel = f"artifacts/sources/{source_id}/broken.txt"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ok\x00text \xff\xfe tail\x00")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel, checksum="broken",
    )

    scan = store.scan_unreadable_text()
    store.close()

    assert [(a.status, a.nuls) for a in _only_source(scan).artifacts] == [
        ("file_undecodable", 0)
    ]


# --- clean sources are still listed, and still say what ingest recorded ----


def test_clean_kb_reports_no_findings(tmp_path):
    store = _store(tmp_path)
    result = store_source(store, tmp_path, "doc.txt", b"body", "body", "text")
    create_chunked_extraction_job(
        store, source_id=result["source_id"], artifact_id=result["artifact_id"],
        source_text=result["text"], provider=None, model=None,
    )

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert [a.status for a in source.artifacts] == ["clean"]
    assert source.chunks == ()
    assert scan.artifacts_scanned == 1
    assert scan.chunks_scanned > 0


def test_clean_source_is_reported_as_scanned(tmp_path, monkeypatch, capsys):
    """"Scanned and clean" and "not looked at" are different answers.

    Dropping clean sources from the report would leave a reader unable to tell
    which of the two a missing line meant.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    store_source(store, tmp_path, "doc.txt", b"body", "body", "text")
    store.close()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "sources/doc.txt: no unreadable characters still stored" in out
    assert "across 1 source(s)" in out


def test_clean_source_names_what_ingest_recorded(tmp_path, monkeypatch, capsys):
    """The commonest post-#473 source: 71 replaced at ingest, none still stored.

    A bare "no unreadable characters still stored" is true and unreadable-proof
    to over-read as "this document lost nothing". The line has to keep ingest's
    number visible beside its own.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/measured.pdf", kind="binary")
    rel = _write_artifact(tmp_path, source_id, "measured", "clean text")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel,
        checksum="measured", unreadable_chars=71,
    )
    store.close()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert (
        "sources/measured.pdf: no unreadable characters still stored — "
        "extraction recorded 71 replaced at ingest" in out
    )


def test_report_keeps_recorded_and_found_apart(tmp_path, monkeypatch, capsys):
    """An artifact that measured 0 and whose file holds 2.

    Reachable: `unreadable_chars` is the count of what the *extraction*
    replaced, so a row written by a clean extraction and a file that later
    diverged from it disagree. Printing one number for both would make the
    report unable to say which it meant.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/mixed.pdf", kind="binary")
    rel = _write_artifact(tmp_path, source_id, "mixed", _DIRTY)
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel,
        checksum="mixed", unreadable_chars=0,
    )

    scan = store.scan_unreadable_text()
    store.close()
    artifact = _only_source(scan).artifacts[0]
    assert (artifact.nuls, artifact.recorded) == (_DIRTY_NULS, 0)

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "artifacts: 2 in the latest of 1 artifact row(s)" in out
    assert "extraction recorded 0 replaced at ingest" in out


def test_source_with_no_stored_text_is_named(tmp_path, monkeypatch, capsys):
    """`extract.py`'s loose-file path registers a source with no artifact row.

    Omitting it would be the strongest form of "not looked at, and not said so",
    and would make the summary's source count a denominator missing its own rows.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    store.add_source("sources/loose.txt")

    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert source.status == "no_stored_text"
    assert (source.artifacts, source.chunks) == ((), ())

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "sources/loose.txt: no stored extraction text — not scanned" in out
    assert "1 with no stored text" in out


# --- superseded rows are not the ones a finding should be reported on -------


def test_superseded_artifact_is_named_as_superseded(tmp_path, monkeypatch, capsys):
    """A user who already re-uploaded must not read a finding about dead rows.

    This command recommends nothing; `sources.html` is what names
    delete-and-re-upload, and `store.delete_source` drops every fact from the
    source including `confirmed` and `accepted` ones. So the harm here is not a
    line telling anyone to delete anything -- it is a report that presents a
    superseded row the same way as a live one, leaving a user whose KB is
    already fixed to act on it through the surface that does recommend.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _, old_id, new_id, _, _ = _repaired_source(store, tmp_path, name="repaired")

    scan = store.scan_unreadable_text()
    store.close()

    by_id = {a.artifact_id: a for a in _only_source(scan).artifacts}
    assert by_id[old_id].status == "found" and by_id[old_id].is_latest is False
    assert by_id[new_id].status == "clean" and by_id[new_id].is_latest is True

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "sources/repaired: unreadable characters in superseded rows only" in out
    assert "no re-upload is called for" in out
    assert "artifacts: 2 in 1 superseded of 2 artifact row(s)" in out


def test_superseded_chunk_is_named_as_superseded(tmp_path):
    store = _store(tmp_path)
    _, _, _, old_job_id, new_job_id = _repaired_source(store, tmp_path, name="repaired")

    scan = store.scan_unreadable_text()
    store.close()

    chunks = _only_source(scan).chunks
    assert chunks, "the fixture planted no dirty chunk"
    assert {c.job_id for c in chunks} == {old_job_id}
    assert all(c.is_latest_job is False for c in chunks)


def test_a_newest_job_with_no_chunks_does_not_promote_a_superseded_one(tmp_path):
    """Job recency comes from `extraction_jobs`, not from the chunk rows.

    `create_chunked_extraction_job` finishes a job immediately when its text
    chunks to nothing, so a source's newest job can own no `source_chunks` rows
    at all. `MAX(job_id)` over `source_chunks` then names the *superseded* job
    as the newest, and every dirty chunk under it reads as a live finding —
    which is the report telling a user whose current job is clean to delete the
    source and re-upload it.
    """
    store = _store(tmp_path)
    source_id, _, old_job_id = _legacy_source(store, tmp_path, name="legacy", text=_DIRTY)
    new_job_id = create_chunked_extraction_job(
        store, source_id=source_id, artifact_id=None,
        source_text="   ", provider=None, model=None,
    )
    assert new_job_id > old_job_id
    assert store.source_chunks(new_job_id) == [], (
        "the newer job owns chunk rows, so this fixture cannot separate the two "
        "readings of 'the newest job'"
    )
    probe = sqlite3.connect(tmp_path / "kb.sqlite")
    highest_job_id_in_chunks = probe.execute(
        "SELECT MAX(job_id) FROM source_chunks WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    probe.close()
    assert highest_job_id_in_chunks == old_job_id

    scan = store.scan_unreadable_text()
    store.close()

    chunks = _only_source(scan).chunks
    assert chunks, "the fixture planted no dirty chunk"
    assert all(c.is_latest_job is False for c in chunks)


def _kb_with_chunks_but_no_jobs(tmp_path):
    """A KB holding dirty chunks whose `extraction_jobs` table is absent.

    Hand-rolled: `extraction_jobs` and `source_chunks` are both plain
    `CREATE TABLE IF NOT EXISTS` in `schema.sql` with no ALTER, so a real KB has
    both or neither and I could not construct this state through any migration
    path. What the two tests below pin is the contract, at the two levels it has
    to hold at — the `Store` method must not call an underivable recency
    `False`, and the CLI line must not turn that into reassurance.
    """
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.executescript(_UNMIGRATED_SCHEMA)
    conn.executescript(
        """
        CREATE TABLE source_chunks (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO sources(path, kind) VALUES('sources/legacy.pdf', 'binary')")
    conn.execute(
        "INSERT INTO source_chunks(source_id, job_id, chunk_index, text) VALUES(1, 1, 0, ?)",
        (_DIRTY,),
    )
    conn.commit()
    conn.close()


def test_chunk_recency_is_unknown_without_an_extraction_jobs_table(tmp_path):
    """No `extraction_jobs` table means recency cannot be derived — not that
    every finding is superseded."""
    _kb_with_chunks_but_no_jobs(tmp_path)

    store = Store(tmp_path / "kb.sqlite")
    scan = store.scan_unreadable_text()
    store.close()

    assert scan.jobs_table_present is False
    chunks = _only_source(scan).chunks
    assert [c.is_latest_job for c in chunks] == [None]
    assert [c.nuls for c in chunks] == [_DIRTY_NULS]


def test_unknown_chunk_recency_is_not_reported_as_reassurance(tmp_path, monkeypatch, capsys):
    """The `Store` contract's CLI consequence, which is where the harm lands.

    `_source_scan_verdict`'s docstring says an underivable recency counts as
    current because not knowing must not read as reassurance. The sibling above
    stops at the method and never reaches that function, so without this the
    clause is a declared choice with nothing behind it: treating `None` as "not
    the latest job" leaves the whole suite green and prints "unreadable
    characters in superseded rows only — nothing re-analysis will read again, so
    no re-upload is called for" about a chunk whose job it never identified.
    """
    _env(monkeypatch, tmp_path)
    _kb_with_chunks_but_no_jobs(tmp_path)

    assert cli.main(["sources", "scan-unreadable"]) == 0
    line = _source_line(capsys.readouterr().out, "sources/legacy.pdf")
    assert line.startswith("sources/legacy.pdf: unreadable characters still stored — ")
    assert "no re-upload is called for" not in line
    assert "in superseded rows only" not in line


def test_an_unread_latest_artifact_is_not_reported_as_an_absence(
    tmp_path, monkeypatch, capsys
):
    """"We could not look" must not round down to "there is nothing there".

    Two shapes, because they round down to two different false sentences. The
    first source's only artifact file is gone, so a `clean` headline would say
    "no unreadable characters still stored" about a file nobody read. The
    second's latest artifact is gone above a dirty superseded one, so a
    `superseded` headline would say "nothing re-analysis will read again" about
    the row re-analysis is precisely the one that would read. Neither may claim
    an absence — and neither may take the `superseded` headline's reassurance,
    which is the one remedy word this command prints and is the assertion these
    lines have not earned.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)

    sole = store.add_source("sources/sole.pdf", kind="binary")
    rel = _write_artifact(tmp_path, sole, "sole", "")
    store.add_source_artifact(
        source_id=sole, kind="extracted_text", path=rel, checksum="sole",
    )
    (tmp_path / rel).unlink()

    half = store.add_source("sources/half.pdf", kind="binary")
    old_rel = _write_artifact(tmp_path, half, "old", _DIRTY)
    store.add_source_artifact(
        source_id=half, kind="extracted_text", path=old_rel, checksum="half-old",
    )
    new_rel = _write_artifact(tmp_path, half, "new", "")
    newer = store.add_source_artifact(
        source_id=half, kind="extracted_text", path=new_rel, checksum="half-new",
    )
    (tmp_path / new_rel).unlink()

    scan = store.scan_unreadable_text()
    store.close()
    by_path = {s.path: s for s in scan.sources}
    latest = [a for a in by_path["sources/half.pdf"].artifacts if a.is_latest]
    assert [(a.artifact_id, a.status) for a in latest] == [(newer, "file_missing")], (
        "the fixture's unread artifact is not the latest row, so it cannot "
        "undermine the verdict and this test would prove nothing"
    )

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    for path in ("sources/sole.pdf", "sources/half.pdf"):
        line = _source_line(out, path)
        assert line.startswith(
            f"{path}: could not tell whether unreadable characters are still stored — "
        )
        assert "no unreadable characters still stored" not in line
        assert "no re-upload is called for" not in line
        assert "in superseded rows only" not in line
        # The caveat still names the row that could not be read.
        assert "could not scan 1 artifact(s)" in line


def test_an_unreadable_superseded_artifact_does_not_cloud_a_clean_latest_one(
    tmp_path, monkeypatch, capsys
):
    """The `unknown` verdict is scoped to the LATEST artifact, and that matters.

    Superseded artifact files going missing is the ordinary state of a KB that
    has been re-uploaded, so a verdict that fired on any unreadable artifact
    would make "could not tell" the common answer and the command noise. The
    latest row is the one re-analysis reads (`latest_source_text_artifact` takes
    MAX(id)), and the scan read it, so this source's absence of findings is one
    the scan actually established.

    `test_an_unread_latest_artifact_is_not_reported_as_an_absence` covers the
    other direction. Neither can stand in for this one: both of its fixtures
    make the unreadable artifact the latest row, so dropping the `is_latest`
    qualifier leaves them green.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/reuploaded.pdf", kind="binary")
    gone_rel = _write_artifact(tmp_path, source_id, "old", "stale")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=gone_rel, checksum="old",
    )
    (tmp_path / gone_rel).unlink()
    latest_rel = _write_artifact(tmp_path, source_id, "new", "clean text")
    latest = store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=latest_rel, checksum="new",
    )

    scan = store.scan_unreadable_text()
    store.close()
    by_id = {a.artifact_id: a for a in _only_source(scan).artifacts}
    assert by_id[latest].is_latest is True and by_id[latest].status == "clean", (
        "the readable artifact is not the latest row, so this fixture cannot "
        "separate the scoped clause from the broad one"
    )

    assert cli.main(["sources", "scan-unreadable"]) == 0
    line = _source_line(capsys.readouterr().out, "sources/reuploaded.pdf")
    assert line.startswith(
        "sources/reuploaded.pdf: no unreadable characters still stored — "
    )
    assert "could not tell" not in line


def test_a_live_finding_outranks_an_unread_latest_artifact(
    tmp_path, monkeypatch, capsys
):
    """Not knowing about one store must not withhold what the other store said.

    `_source_scan_verdict` puts the live-chunk clause above the unknown-artifact
    clause deliberately. Reversed, this source — whose latest artifact could not
    be read AND whose newest job holds a chunk with NULs the scan did read —
    would report "could not tell" and drop a finding it is certain of.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/both.pdf", kind="binary")
    gone_rel = _write_artifact(tmp_path, source_id, "gone", "stale")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=gone_rel, checksum="gone",
    )
    (tmp_path / gone_rel).unlink()
    job_id = store.create_extraction_job(
        source_id=source_id, artifact_id=None, provider=None, model=None,
        total_chunks=1, message="",
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=[_DIRTY])

    scan = store.scan_unreadable_text()
    store.close()
    source = _only_source(scan)
    assert [a.status for a in source.artifacts] == ["file_missing"]
    assert [c.is_latest_job for c in source.chunks] == [True], (
        "the chunk is not on the latest job, so this fixture cannot separate "
        "the two clauses' order"
    )

    assert cli.main(["sources", "scan-unreadable"]) == 0
    line = _source_line(capsys.readouterr().out, "sources/both.pdf")
    assert line.startswith("sources/both.pdf: unreadable characters still stored — ")
    assert f"chunks: {_DIRTY_NULS} in 1 chunk(s) of the latest job" in line
    assert "could not tell" not in line


def test_recorded_prints_beside_a_finding_under_the_unknown_headline(
    tmp_path, monkeypatch, capsys
):
    """"measured 0" stays distinguishable from "never measured" on every line
    that reports a finding — not only on the ones whose headline is `current`.

    Keying the suppression on the verdict name instead of on finding-ness
    silently dropped this clause when the fourth verdict arrived. The source
    below carries a real finding (a dirty superseded artifact) under the
    `unknown` headline, and both of its rows recorded 0, so without the number
    the line is byte-identical to the same source with `unreadable_chars` NULL.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/measured.pdf", kind="binary")
    dirty_rel = _write_artifact(tmp_path, source_id, "dirty", _DIRTY)
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=dirty_rel,
        checksum="dirty", unreadable_chars=0,
    )
    gone_rel = _write_artifact(tmp_path, source_id, "gone", "stale")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=gone_rel,
        checksum="gone", unreadable_chars=0,
    )
    (tmp_path / gone_rel).unlink()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    line = _source_line(capsys.readouterr().out, "sources/measured.pdf")
    assert line.startswith(
        "sources/measured.pdf: could not tell whether unreadable characters are "
        "still stored — "
    )
    assert f"artifacts: {_DIRTY_NULS} in 1 superseded of 2 artifact row(s)" in line
    assert "extraction recorded 0 replaced at ingest" in line


def test_a_line_with_no_finding_stays_quiet_about_a_measured_zero(
    tmp_path, monkeypatch, capsys
):
    """A measured, lossless source has nothing to report, and reports nothing.

    Asserted as whole lines. A substring assertion cannot see this property at
    all: `"...no unreadable characters still stored" in out` is satisfied just as
    well by a line that goes on to say "— extraction recorded 0 replaced at
    ingest", so it leaves "stays quiet" untested.

    Both findingless headlines are here, because the suppression covers both and
    a rule keyed on one of them would leave the other chattering.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)

    clean_id = store.add_source("sources/clean.pdf", kind="binary")
    clean_rel = _write_artifact(tmp_path, clean_id, "clean", "clean text")
    store.add_source_artifact(
        source_id=clean_id, kind="extracted_text", path=clean_rel,
        checksum="clean", unreadable_chars=0,
    )

    unread_id = store.add_source("sources/unread.pdf", kind="binary")
    unread_rel = _write_artifact(tmp_path, unread_id, "unread", "stale")
    store.add_source_artifact(
        source_id=unread_id, kind="extracted_text", path=unread_rel,
        checksum="unread", unreadable_chars=0,
    )
    (tmp_path / unread_rel).unlink()
    store.close()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert (
        _source_line(out, "sources/clean.pdf")
        == "sources/clean.pdf: no unreadable characters still stored"
    )
    assert _source_line(out, "sources/unread.pdf") == (
        "sources/unread.pdf: could not tell whether unreadable characters are "
        f"still stored — could not scan 1 artifact(s) — file_missing: {unread_rel}"
    )


def test_the_summary_counts_the_sources_the_scan_could_not_answer_for(
    tmp_path, monkeypatch, capsys
):
    """Every verdict gets a bucket, so `unknown` sources can be counted.

    Without one, an `unknown` source shows up only under "have an artifact that
    could not be read" — a different and broader question, which the `current`
    source below also answers yes to because of its own missing superseded row.
    One bucket standing for two verdicts cannot be read either way round.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)

    live = store.add_source("sources/live.pdf", kind="binary")
    stale_rel = _write_artifact(tmp_path, live, "stale", "stale")
    store.add_source_artifact(
        source_id=live, kind="extracted_text", path=stale_rel, checksum="stale",
    )
    (tmp_path / stale_rel).unlink()
    dirty_rel = _write_artifact(tmp_path, live, "dirty", _DIRTY)
    store.add_source_artifact(
        source_id=live, kind="extracted_text", path=dirty_rel, checksum="dirty",
    )

    unread = store.add_source("sources/unread.pdf", kind="binary")
    gone_rel = _write_artifact(tmp_path, unread, "gone", "stale")
    store.add_source_artifact(
        source_id=unread, kind="extracted_text", path=gone_rel, checksum="gone",
    )
    (tmp_path / gone_rel).unlink()
    store.close()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert _source_line(out, "sources/live.pdf").startswith(
        "sources/live.pdf: unreadable characters still stored — "
    )
    assert _source_line(out, "sources/unread.pdf").startswith(
        "sources/unread.pdf: could not tell "
    )
    assert "1 with unreadable characters in current rows" in out
    assert "1 whose latest stored text could not be read" in out
    # The broader question still has its own count, still names both sources,
    # and is still marked off from the verdict buckets. Asserted as the whole
    # clause: `"2 source(s) had ..."` alone survives deleting `separately,`,
    # which is the word saying this number is not part of the list before it.
    assert "; separately, 2 source(s) had an artifact that could not be read" in out


def test_a_kb_with_no_source_artifacts_table_is_named_not_called_clean(
    tmp_path, monkeypatch, capsys
):
    """A KB predating `source_artifacts` scans zero artifacts and says so.

    `_KB_CORE_TABLES`' comment records that `source_artifacts` arrived by
    migration, so this KB passes `_require_existing_kb` — asserted below. The
    probe is what keeps `SELECT ... FROM source_artifacts` from raising, and
    nothing else in this file drops that table, so without this test the probe
    can be replaced by `True` and stay green.
    """
    _env(monkeypatch, tmp_path)
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.executescript(
        _UNMIGRATED_SCHEMA[: _UNMIGRATED_SCHEMA.index("CREATE TABLE source_artifacts")]
    )
    conn.execute("INSERT INTO sources(path, kind) VALUES('sources/old.txt', 'text')")
    conn.commit()
    conn.close()
    assert cli._kb_schema_problem(tmp_path / "kb.sqlite") is None

    store = Store(tmp_path / "kb.sqlite")
    scan = store.scan_unreadable_text()
    store.close()
    assert scan.artifacts_table_present is False
    assert scan.artifacts_scanned == 0
    assert _only_source(scan).status == "no_stored_text"

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "no source_artifacts table" in out
    assert "scanned 0 extraction-text artifact(s)" in out


def test_recorded_prints_beside_a_chunk_only_finding(tmp_path, monkeypatch, capsys):
    """A finding in the chunk store keeps the recorded number on the line too.

    `_recorded_clause` asks whether the LINE reports a finding, and a line can
    report one from either store. This source's artifact is clean and measured
    0, and its finding is a dirty chunk in the latest job — so with only the
    artifact half of that question the clause disappears and the line stops
    distinguishing "measured 0" from "never measured", which is the property
    the docstring names.

    `test_recorded_prints_beside_a_finding_under_the_unknown_headline` covers
    the artifact half; neither can stand in for the other, because each is the
    other's deleted disjunct.
    """
    _env(monkeypatch, tmp_path)
    store = _store(tmp_path)
    source_id = store.add_source("sources/chunky.pdf", kind="binary")
    rel = _write_artifact(tmp_path, source_id, "clean", "clean text")
    store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path=rel,
        checksum="clean", unreadable_chars=0,
    )
    job_id = store.create_extraction_job(
        source_id=source_id, artifact_id=None, provider=None, model=None,
        total_chunks=1, message="",
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=[_DIRTY])

    scan = store.scan_unreadable_text()
    store.close()
    source = _only_source(scan)
    assert [a.status for a in source.artifacts] == ["clean"], (
        "an artifact finding would let the artifact disjunct carry this test "
        "on its own, and the chunk disjunct would go unpinned again"
    )
    assert [c.nuls for c in source.chunks] == [_DIRTY_NULS]

    assert cli.main(["sources", "scan-unreadable"]) == 0
    line = _source_line(capsys.readouterr().out, "sources/chunky.pdf")
    assert "extraction recorded 0 replaced at ingest" in line


def test_only_extraction_text_artifacts_are_scanned(tmp_path, monkeypatch, capsys):
    """The scan reads extraction text, not every row in `source_artifacts`.

    `latest_source_text_artifact` and `source_text_inputs` both filter to
    `original_text`/`extracted_text`, so the scan filtering the same way is what
    keeps `is_latest` naming the row those two would actually return. The report
    also calls what it counted "extraction-text artifact(s)". The filter is
    forward cover: if the CHECK is ever widened, a new kind must not silently
    start being opened as text.

    NO REAL KB CAN HOLD A THIRD KIND, so what this test rests on is a divergence
    between `_UNMIGRATED_SCHEMA` and any KB verinote has ever written: the
    missing CHECK on `kind`, an artefact of hand-rolling. The missing
    `unreadable_chars` column is NOT one of those — a real pre-#473 KB lacks it
    too, which is exactly what that fixture exists to reproduce.

    Do not read the fixture as faithful apart from that. It is a hand-rolled
    minimum shaped to satisfy `_require_existing_kb`, and it diverges from the
    real schema in tables, columns and constraints this test never touches.
    `init_schema()` neither leaves it alone nor cleanly refuses it: measured on
    this fixture, it creates tables and adds `unreadable_chars`, and only then
    raises, on an index over a column the hand-rolled `facts` lacks.

    `test_scan_writes_nothing[True]` does catch an accidental `init_schema()`
    here, but by the RAISE rather than by its snapshot -- the raise escapes, or
    the command's floor turns it into a non-zero return code, and either way
    `assert cli.main(...) == 0` trips one line before the snapshot is compared.
    Measured both ways round: suppress the raise and the snapshot does catch the
    landed migration; raise the same error before any write and the test still
    fails. So the ordering above is described, not pinned -- were
    `init_schema()` ever to become inert on this fixture, that mutant would
    return 0 with an identical snapshot and the test would go quietly green.

    Both creation paths have always carried the constraint: `schema.sql` since
    `source_artifacts` was introduced in `2bb1aa4` ("Add source text
    artifacts"), and `_ensure_schema_migrations`' own CREATE — the path an
    existing KB goes through — in every form it has had. `schema.sql` is
    `CREATE TABLE IF NOT EXISTS`, so no migration could have left an older KB a
    CHECK-free copy either, and `add_source_artifact(kind="sidecar")` on a real
    KB raises `CHECK constraint failed`.

    So this pins a contract, not a reachable state, the way
    `test_chunk_recency_is_unknown_without_an_extraction_jobs_table` does.

    `test_legacy_original_text_artifact_is_scanned` pins the filter's other
    edge, that it must not narrow to `extracted_text` alone.
    """
    _env(monkeypatch, tmp_path)
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.executescript(_UNMIGRATED_SCHEMA)
    conn.execute("INSERT INTO sources(path, kind) VALUES('sources/mixed.pdf', 'binary')")
    source_id = int(conn.execute("SELECT id FROM sources").fetchone()[0])
    text_rel = _write_artifact(tmp_path, source_id, "text", _DIRTY)
    other_rel = _write_artifact(tmp_path, source_id, "other", _DIRTY)
    conn.executemany(
        "INSERT INTO source_artifacts(source_id, kind, path, checksum) VALUES(?,?,?,?)",
        [(source_id, "extracted_text", text_rel, "t"), (source_id, "sidecar", other_rel, "o")],
    )
    conn.commit()
    conn.close()

    store = Store(tmp_path / "kb.sqlite")
    scan = store.scan_unreadable_text()
    store.close()

    source = _only_source(scan)
    assert [a.path for a in source.artifacts] == [text_rel], (
        "the non-extraction-text row was scanned, so the scan is reading rows "
        "no extraction path would ever read as text"
    )
    assert scan.artifacts_scanned == 1

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    # No assertion that the sidecar PATH is absent: paths reach stdout from one
    # place, the `could not scan` detail, and only for a row that failed to be
    # read. This sidecar file is readable, so under the mutation this test
    # guards it is scanned as `found` and its path is never printed either way.
    assert "scanned 1 extraction-text artifact(s)" in out


# --- the unmigrated KB, which is the population #494 exists for ------------


def test_scan_finds_nuls_on_unmigrated_kb(tmp_path, monkeypatch, capsys):
    """The acceptance property: a KB written before #473 is found and reported.

    Such a KB has no `unreadable_chars` column and no `source_chunks` table, and
    it passes `_require_existing_kb` — asserted below, because a fixture that
    could not get past the refusal would prove nothing. Selecting the column
    unconditionally raises `no such column`; querying the table raises `no such
    table`. Either way the one command written for these users would answer with
    a traceback.
    """
    _env(monkeypatch, tmp_path)
    _unmigrated_kb(tmp_path)
    assert cli._kb_schema_problem(tmp_path / "kb.sqlite") is None, (
        "this fixture does not pass _require_existing_kb, so it is not the "
        "reachable state the test claims to cover"
    )

    store = Store(tmp_path / "kb.sqlite")
    scan = store.scan_unreadable_text()
    store.close()

    assert scan.artifacts_table_present is True
    assert scan.chunks_table_present is False
    assert scan.unreadable_chars_column_present is False
    artifact = _only_source(scan).artifacts[0]
    assert (artifact.status, artifact.nuls, artifact.recorded) == (
        "found", _DIRTY_NULS, None,
    )
    assert scan.chunks_scanned == 0

    assert cli.main(["sources", "scan-unreadable"]) == 0
    out = capsys.readouterr().out
    assert "no source_chunks table" in out
    assert "predates the unreadable_chars column" in out
    assert "artifacts: 2 in the latest of 1 artifact row(s)" in out


@pytest.mark.parametrize("unmigrated", [False, True])
def test_scan_writes_nothing(tmp_path, monkeypatch, unmigrated):
    """Nothing on disk or in the KB differs after a scan, on either schema.

    Both fixtures, because each pins a half the other cannot. On the unmigrated
    KB an accidental `init_schema()` shows up in `sqlite_master` and in
    `table_info`, and a write-back of the count cannot even be attempted
    because the column does not exist. On the migrated KB `init_schema()` is a
    no-op over both snapshots (it only ever adds what is already there), but a
    write-back succeeds silently and the data snapshot is the only thing that
    catches it. Measured on this tree: an accidental `init_schema()` in the
    command leaves the migrated parameter green and reddens the unmigrated one;
    a write-back reddens both, and only the migrated one by the snapshot.

    Driven through `cli.main` and not only through the method, because the two
    ways this command could write are in different files: the method's SQL, and
    an `init_schema()` on the Store the command opens.
    """
    _env(monkeypatch, tmp_path)
    if unmigrated:
        _unmigrated_kb(tmp_path)
    else:
        store = _store(tmp_path)
        _legacy_source(store, tmp_path, name="legacy", text=_DIRTY)
        store.close()

    def snapshot():
        conn = sqlite3.connect(tmp_path / "kb.sqlite")
        conn.row_factory = sqlite3.Row
        state = {
            "master": [tuple(r) for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )],
            "artifact_columns": [
                r["name"] for r in conn.execute("PRAGMA table_info(source_artifacts)")
            ],
        }
        if "unreadable_chars" in state["artifact_columns"]:
            state["recorded"] = [tuple(r) for r in conn.execute(
                "SELECT id, unreadable_chars FROM source_artifacts ORDER BY id"
            )]
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_chunks'"
        ).fetchone():
            state["chunks"] = [tuple(r) for r in conn.execute(
                "SELECT id, text FROM source_chunks ORDER BY id"
            )]
        conn.close()
        state["files"] = sorted(
            (str(p.relative_to(tmp_path)), p.read_bytes())
            for p in (tmp_path / "artifacts").rglob("*") if p.is_file()
        )
        return state

    before = snapshot()
    store = Store(tmp_path / "kb.sqlite")
    scan = store.scan_unreadable_text()
    store.close()

    assert any(a.status == "found" for s in scan.sources for a in s.artifacts), (
        "the scan found nothing, so a write-back mutant would have nothing to write"
    )
    assert cli.main(["sources", "scan-unreadable"]) == 0
    assert snapshot() == before


# --- the command's floors --------------------------------------------------


def test_scan_unreadable_runs_on_halted_kb(tmp_path, monkeypatch, capsys):
    """A halt the user cannot diagnose is a bricked KB.

    `_refuse_on_halted_kb` opens a Store and calls `init_schema()` on it, so a
    non-`halt_safe` spelling would migrate the KB before this read-only scan of
    it ran.
    """
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    (tmp_path / POLICY_RELPATH).unlink()
    capsys.readouterr()

    assert cli.main(["sources", "scan-unreadable"]) == 0
    assert "extraction-text artifact(s)" in capsys.readouterr().out


def test_scan_unreadable_refuses_missing_kb(tmp_path, monkeypatch, capsys):
    """A mistyped root must not be scaffolded into a KB that then reports clean."""
    _env(monkeypatch, tmp_path)

    assert cli.main(["sources", "scan-unreadable"]) == 1
    assert "no KB at" in capsys.readouterr().err
    assert not (tmp_path / "kb.sqlite").exists()


def test_unreadable_scan_reports_an_unreadable_kb(tmp_path, monkeypatch, capsys):
    """A KB that gets past `_require_existing_kb` and fails deeper in.

    `source_artifacts` here has no `kind` column — a shape the probes do not
    cover, standing in for the next column someone forgets. The floor is what
    turns it into a diagnosis instead of a traceback.
    """
    _env(monkeypatch, tmp_path)
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.executescript(_UNMIGRATED_SCHEMA.replace("kind TEXT NOT NULL,\n", "", 1))
    conn.commit()
    conn.close()
    assert cli._kb_schema_problem(tmp_path / "kb.sqlite") is None
    assert "kind" not in {
        row[1]
        for row in sqlite3.connect(tmp_path / "kb.sqlite").execute(
            "PRAGMA table_info(source_artifacts)"
        )
    }

    assert cli.main(["sources", "scan-unreadable"]) == 1
    assert "cannot read the KB at" in capsys.readouterr().err
