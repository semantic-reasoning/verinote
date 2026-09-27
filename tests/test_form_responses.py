# SPDX-License-Identifier: MPL-2.0
"""#478: a form sheet's response batch materialises as a first-class source.

Every test injects the ``read`` seam with a synthetic ``SheetReadResult``
(``Sample*`` tokens, ``example.com``), so the base suite never dials Google.
One test pins the *default* path against the network seam itself: if the real
client were ever called from a test, the transport seam would raise
``AssertionError`` instead of opening a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verinote.config import GoogleGrant
from verinote.google_sheets import (
    SheetReadError,
    SheetReadResult,
    TokenRefreshError,
)
from verinote.pipeline import (
    DEFAULT_VALUE_RANGE,
    IngestError,
    ResponseBatch,
    fetch_form_responses,
    materialize_form_batch,
    parse_watermark,
    render_responses_as_text,
)
from verinote.store import Store

REFRESH_TOKEN = "synthetic-refresh-token-1"
ROTATED_TOKEN = "synthetic-rotated-token-2"
CLIENT_ID = "sample-client-id"
SHEET_ID = "sample_sheet_1"
HEADER = ("Timestamp", "Question one", "Question two")


def _grant() -> GoogleGrant:
    return GoogleGrant(
        refresh_token=REFRESH_TOKEN,
        email="sample@example.com",
        scopes=("https://www.googleapis.com/auth/spreadsheets.readonly",),
    )


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _read_stub(rows: tuple[tuple[str, ...], ...], rotated: str | None = None):
    """The injected seam: the client's exact 4-argument signature, no network."""

    def read(grant, client_id, sheet_id, value_range):
        read.calls.append((grant, client_id, sheet_id, value_range))  # type: ignore[attr-defined]
        return SheetReadResult(rows=rows, rotated_refresh_token=rotated)

    read.calls = []  # type: ignore[attr-defined]
    return read


# --- (a) round-trip ----------------------------------------------------------


def test_materialize_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
        ("2026-09-27 11:30", "Sample Answer C", "Sample Answer D"),
    )
    read = _read_stub(rows)
    result = materialize_form_batch(
        store,
        tmp_path,
        _grant(),
        CLIENT_ID,
        SHEET_ID,
        since_watermark=None,
        read=read,
        stamp="20260927T000000Z",
    )
    assert result["materialized"] is True
    assert result["response_count"] == 2
    assert result["new_watermark"] == "2"
    assert result["citation"] == f"sources/form-{SHEET_ID}-20260927T000000Z.txt"
    # the original (rendered) batch is on disk under sources/
    assert (tmp_path / "sources" / f"form-{SHEET_ID}-20260927T000000Z.txt").is_file()
    # one source row, one artifact row
    src = store.get_source(result["source_id"])
    assert src is not None
    assert src["path"] == result["citation"]
    assert src["kind"] == "text"
    art = store.get_source_artifact(result["artifact_id"])
    assert art is not None
    assert art["kind"] == "extracted_text"
    # a pending job with the batch's chunks
    job = store.get_extraction_job(result["job_id"])
    assert job is not None
    assert job["status"] == "pending"
    assert job["total_chunks"] >= 1
    chunks = store.source_chunks(result["job_id"])
    assert len(chunks) == int(job["total_chunks"])
    joined = "".join(c["text"] for c in chunks)
    assert "Question one=Sample Answer A" in joined
    assert "submitted 2026-09-27 10:00" in joined
    # the seam was used exactly once, with the client's real argument shape
    assert len(read.calls) == 1  # type: ignore[attr-defined]
    _g, cid, sid, value_range = read.calls[0]  # type: ignore[attr-defined]
    assert (cid, sid, value_range) == (CLIENT_ID, SHEET_ID, "A1:ZZ")
    assert _g.refresh_token == REFRESH_TOKEN


# --- (b) watermark resume ----------------------------------------------------


def test_watermark_resume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = (HEADER, ("t1", "a", "b"), ("t2", "c", "d"))
    r1 = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(rows), stamp="s1",
    )
    assert r1["materialized"] is True
    assert r1["response_count"] == 2
    assert r1["new_watermark"] == "2"

    # second check: nothing new -> materialises nothing, watermark holds
    r2 = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark="2", read=_read_stub(rows), stamp="s2",
    )
    assert r2["materialized"] is False
    assert r2["response_count"] == 0
    assert r2["new_watermark"] == "2"
    assert r2["source_id"] is None
    assert r2["job_id"] is None
    assert len(store.sources()) == 1
    assert len(store.source_extraction_jobs()) == 1

    # a third row lands: ONLY the new row materialises
    rows3 = rows + (("t3", "e", "f"),)
    r3 = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark="2", read=_read_stub(rows3), stamp="s3",
    )
    assert r3["materialized"] is True
    assert r3["response_count"] == 1
    assert r3["new_watermark"] == "3"
    assert "Question one=e" in r3["text"]
    assert "Question one=a" not in r3["text"]
    assert len(store.sources()) == 2
    assert len(store.source_extraction_jobs()) == 2


# --- (c) NUL sanitising -------------------------------------------------------


def test_nul_replaced_and_counted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = (HEADER, ("t1", "a\x00b", "c"))
    r = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(rows), stamp="s",
    )
    assert r["unreadable_chars"] == 1
    assert "\x00" not in r["text"]
    assert "a\ufffdb" in r["text"]
    # the artifact row carries the count (the #473 idiom)
    art = store.get_source_artifact(r["artifact_id"])
    assert art is not None
    assert art["unreadable_chars"] == 1
    # and the artifact file and the job's chunks are NUL-free
    art_text = (tmp_path / r["artifact_path"]).read_text(encoding="utf-8")
    assert "\x00" not in art_text
    assert "a\ufffdb" in art_text
    joined = "".join(c["text"] for c in store.source_chunks(r["job_id"]))
    assert "\x00" not in joined
    assert "a\ufffdb" in joined


# --- (d) provenance -----------------------------------------------------------


def test_provenance_renders_form_citation(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web import create_app

    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store
    rows = (HEADER, ("t1", "Sample Answer", "x"))
    r = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(rows), stamp="s",
    )
    assert r["materialized"] is True
    fid = store.add_fact(
        "Sample Subject", "rel", "Sample Object",
        status="needs_review", source_id=r["source_id"],
    )
    resp = client.get(f"/facts/{fid}/provenance")
    assert resp.status_code == 200
    assert f"form-{SHEET_ID}-s.txt" in resp.text


# --- (e) edges ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("watermark", "expected"),
    [(None, 0), ("", 0), ("0", 0), ("5", 5), ("42", 42)],
)
def test_parse_watermark_valid(watermark: str | None, expected: int) -> None:
    assert parse_watermark(watermark) == expected


@pytest.mark.parametrize(
    "bad", ["abc", "-1", "1.5", "0x5", "+3", True, 12.5, 3, ["5"]],
)
def test_parse_watermark_invalid(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_watermark(bad)  # type: ignore[arg-type]


def test_shrink_is_the_resume_mechanism(tmp_path: Path) -> None:
    """5 rows read (wm "5"), sheet shrinks to 3: nothing new, wm follows back.

    Then a new row (4) lands and IS read — a "max watermark" would clamp to 5
    and skip row 4 forever (the rejected [H] fix). Pins the CURRENT behaviour.
    """
    rows5 = (HEADER,) + tuple((f"t{i}", f"a{i}", f"b{i}") for i in range(5))
    b0 = fetch_form_responses(
        _grant(), CLIENT_ID, SHEET_ID, since_watermark=None, read=_read_stub(rows5)
    )
    assert b0.new_watermark == "5"
    rows3 = rows5[:4]
    b1 = fetch_form_responses(
        _grant(), CLIENT_ID, SHEET_ID, since_watermark="5", read=_read_stub(rows3)
    )
    assert b1.responses == ()
    assert b1.new_watermark == "3"
    rows4 = rows3 + (("t5", "a5", "b5"),)
    b2 = fetch_form_responses(
        _grant(), CLIENT_ID, SHEET_ID, since_watermark="3", read=_read_stub(rows4)
    )
    assert b2.responses == (("t5", "a5", "b5"),)
    assert b2.new_watermark == "4"


def test_collision_rejected_before_side_effects(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows1 = (HEADER, ("t1", "first batch answer", "x"))
    r1 = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(rows1), stamp="s",
    )
    assert r1["materialized"] is True
    raw = tmp_path / "sources" / f"form-{SHEET_ID}-s.txt"
    first_bytes = raw.read_bytes()
    # same sheet + stamp again (two checks within one clock second): refuse
    rows2 = (HEADER, ("t2", "second batch answer", "y"))
    with pytest.raises(IngestError, match="refusing to merge"):
        materialize_form_batch(
            store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
            since_watermark=None, read=_read_stub(rows2), stamp="s",
        )
    assert len(store.sources()) == 1
    # the first batch's bytes are untouched — no silent overwrite
    assert raw.read_bytes() == first_bytes


def test_rotated_token_propagates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = (HEADER, ("t1", "a", "b"))
    r = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(rows, rotated=ROTATED_TOKEN), stamp="s",
    )
    assert r["rotated_refresh_token"] == ROTATED_TOKEN
    # and on the empty-batch path too (the caller still must persist it)
    r2 = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark="1", read=_read_stub(rows, rotated=ROTATED_TOKEN), stamp="s2",
    )
    assert r2["materialized"] is False
    assert r2["rotated_refresh_token"] == ROTATED_TOKEN


def test_sheet_read_error_propagates_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def read_forbidden(grant, client_id, sheet_id, value_range):
        raise SheetReadError(
            "the sheet could not be read", status=403, retryable=False
        )

    with pytest.raises(SheetReadError) as exc:
        materialize_form_batch(
            store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
            since_watermark=None, read=read_forbidden,
        )
    assert exc.value.status == 403
    assert exc.value.retryable is False
    assert len(store.sources()) == 0
    assert store.source_extraction_jobs() == []


def test_token_refresh_error_propagates_with_live_token(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def read_dead_token(grant, client_id, sheet_id, value_range):
        raise TokenRefreshError(
            "the token exchange failed",
            status=400,
            retryable=False,
            live_refresh_token=ROTATED_TOKEN,
        )

    with pytest.raises(TokenRefreshError) as exc:
        materialize_form_batch(
            store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
            since_watermark=None, read=read_dead_token,
        )
    assert exc.value.status == 400
    assert exc.value.live_refresh_token == ROTATED_TOKEN
    assert len(store.sources()) == 0


def test_render_edges() -> None:
    # empty batch -> "" (the materialise-nothing signal)
    b = ResponseBatch(sheet_id=SHEET_ID, header=HEADER, responses=(), new_watermark="0")
    assert render_responses_as_text(b) == ""
    # first column is not "Timestamp": no submitted context
    b = ResponseBatch(
        sheet_id=SHEET_ID, header=("Q1", "Q2"), responses=(("a", "b"),), new_watermark="1"
    )
    text = render_responses_as_text(b)
    assert "submitted" not in text
    assert "Q1=a; Q2=b" in text
    # empty label -> positional name; empty answer -> (no answer)
    b = ResponseBatch(
        sheet_id=SHEET_ID, header=("", "Q2"), responses=(("a", ""),), new_watermark="1"
    )
    text = render_responses_as_text(b)
    assert "question 1=a" in text
    assert "Q2=(no answer)" in text
    # a cell beyond the header -> named by column
    b = ResponseBatch(
        sheet_id=SHEET_ID, header=("Q1",), responses=(("a", "b"),), new_watermark="1"
    )
    text = render_responses_as_text(b)
    assert "Q1=a" in text
    assert "(column 2)=b" in text
    # Timestamp column present but empty: no submitted part at all
    b = ResponseBatch(
        sheet_id=SHEET_ID, header=HEADER, responses=(("", "a"),), new_watermark="1"
    )
    text = render_responses_as_text(b)
    assert "submitted" not in text
    assert "Question one=a" in text


def test_wide_sheet_renders_every_column() -> None:
    """27 columns fit the default range: ZZ is column 702, not 26.

    Pins the rejected "A1"/"A1:Z" range change: with ``A1`` the whole fetch
    would be ONE cell and every response would silently never materialise.
    """
    n = 27
    header = tuple(f"Q{i}" for i in range(n))
    row = tuple(f"answer-{i}" for i in range(n))
    b = ResponseBatch(sheet_id=SHEET_ID, header=header, responses=(row,), new_watermark="1")
    text = render_responses_as_text(b)
    for i in range(n):
        assert f"Q{i}=answer-{i}" in text
    assert DEFAULT_VALUE_RANGE == "A1:ZZ"


def test_unsafe_sheet_id_sanitised_in_citation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # #486 validates ids upstream; this is the defensive filename re-statement
    rows = (HEADER, ("t1", "a", "b"))
    r = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, "ab/c d:1",
        since_watermark=None, read=_read_stub(rows), stamp="s",
    )
    assert r["citation"] == "sources/form-ab_c_d_1-s.txt"
    assert (tmp_path / "sources" / "form-ab_c_d_1-s.txt").is_file()


def test_empty_sheet_is_a_zero_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    r = materialize_form_batch(
        store, tmp_path, _grant(), CLIENT_ID, SHEET_ID,
        since_watermark=None, read=_read_stub(()), stamp="s",
    )
    assert r["materialized"] is False
    assert r["new_watermark"] == "0"
    assert len(store.sources()) == 0


def test_default_read_reaches_the_network_seam(monkeypatch, tmp_path: Path) -> None:
    """The default ``read`` IS the #486 client, pinned at its transport seam.

    Every other test injects a stub; this one calls the default and proves it
    walks into ``_do_refresh`` (the network seam) instead of skipping the
    client — so a test that forgets its stub fails here rather than dialing
    Google, and a regression that drops the real client is caught too.
    """
    import verinote.google_sheets as gs

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the base suite must not dial Google")

    monkeypatch.setattr(gs, "_do_refresh", _boom)
    store = _store(tmp_path)
    with pytest.raises(AssertionError, match="must not dial Google"):
        materialize_form_batch(
            store, tmp_path, _grant(), CLIENT_ID, SHEET_ID, since_watermark=None
        )
