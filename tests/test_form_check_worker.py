# SPDX-License-Identifier: MPL-2.0
"""#479: the form check worker — assert_writable, error mapping, token rotation.

Every test injects the ``read`` seam with a synthetic ``SheetReadResult`` or
raises a synthetic GoogleSheets exception, so the base suite never dials Google.
Synthetic tokens only (``Sample*``, ``example.com``).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verinote.config import (
    Config,
    GoogleGrant,
    GoogleOAuthCorruptError,
    load_google_grant,
)
from verinote.engine import DEFAULT_POLICY
from verinote.google_sheets import (
    SheetReadError,
    SheetReadResult,
    TokenRefreshError,
)
from verinote.pipeline.policy_state import (
    POLICY_RELPATH,
    PolicyMissingError,
    policy_sha256,
)
from verinote.store import Store
from verinote.web import app as webapp
from verinote.web.app import _run_form_check

REFRESH_TOKEN = "synthetic-refresh-token-1"
ROTATED_TOKEN = "synthetic-rotated-token-2"
CLIENT_ID = "sample-client-id"
SHEET_ID = "sample_sheet_1"
EMAIL = "sample@example.com"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
HEADER = ("Timestamp", "Question one", "Question two")


def _grant() -> GoogleGrant:
    return GoogleGrant(
        refresh_token=REFRESH_TOKEN,
        email=EMAIL,
        scopes=SCOPES,
    )


def _cfg(tmp_path: Path) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )


def _policy_kb(cfg: Config, *, with_policy: bool = True) -> None:
    policy = cfg.root / POLICY_RELPATH
    with Store(cfg.db_path) as store:
        store.init_schema()
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(DEFAULT_POLICY, encoding="utf-8")
        store.record_policy_marker(policy_sha256(DEFAULT_POLICY), origin="scaffold")
    if not with_policy:
        policy.unlink()


def _form_kb(cfg: Config, *, sheet_id: str = SHEET_ID) -> int:
    with Store(cfg.db_path) as store:
        store.init_schema()
        return store.add_form_source(sheet_id, "Sample Form")


def _read_stub(rows=(), rotated=None, exc=None):
    calls = []

    def read(grant, client_id, sheet_id, value_range):
        calls.append((grant, client_id, sheet_id, value_range))
        if exc is not None:
            raise exc
        return SheetReadResult(rows=rows, rotated_refresh_token=rotated)

    read.calls = calls
    return read


def _row(cfg: Config, form_id: int) -> dict:
    with Store(cfg.db_path) as store:
        store.init_schema()
        return dict(store.get_form_source(form_id))


def _stub_google_transport(monkeypatch, rows=()):
    """Pin the google_sheets transport seam so a background worker never
    dials Google.

    create_app() starts the resume worker in a daemon thread with the REAL
    read_sheet_values; any test that lets the credential gates pass must stub
    _open/_sleep BEFORE create_app(cfg), or that worker will POST to the real
    OAuth endpoint (the base-suite "never dials Google" invariant).
    """
    import json as _json

    import verinote.google_sheets as gs

    class _Resp:
        def __init__(self, data: bytes):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return self.data

    def _fake_open(req, *, timeout):
        if "token" in req.full_url:
            payload = _json.dumps(
                {"access_token": "synthetic-access", "expires_in": 3600}
            ).encode()
            return _Resp(payload)
        payload = _json.dumps({"values": [list(r) for r in rows]}).encode()
        return _Resp(payload)

    monkeypatch.setattr(gs, "_open", _fake_open)
    monkeypatch.setattr(gs, "_sleep", lambda s: None)


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    """Poll until `predicate()` is truthy; raise if it is not by the deadline.

    (The old form did `assertion(); return` and only caught AssertionError,
    so a boolean-returning caller never actually waited — the resume test it
    served passed vacuously. A predicate that stays false must fail the test.)
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(
        f"predicate not satisfied within {timeout:.1f}s"
    )


# --- required tests ----------------------------------------------------------


def test_check_success_materializes_source_and_job(
    tmp_path, monkeypatch
):
    """Required (a): stub sheet client, 1 check → source and extraction job created.

    (Extraction launch itself is the _start_source_extraction closure in
    create_app, not part of _run_form_check, so no get_client stub is needed
    here — start_extraction defaults to None.)
    """
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    lock = threading.Lock()
    _run_form_check(
        Store(cfg.db_path), cfg, fid, sheet_lock=lock, read=_read_stub(rows)
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] is None
    assert row["watermark"] is not None
    # The source and extraction job were materialised (extraction launch is
    # handled by the _start_source_extraction closure in create_app, not by
    # _run_form_check itself).
    with Store(cfg.db_path) as store:
        store.init_schema()
        sources = list(store._conn.execute("SELECT * FROM sources"))
        assert len(sources) >= 1
        jobs = list(store._conn.execute("SELECT * FROM extraction_jobs"))
        assert len(jobs) >= 1


def test_check_on_halted_kb_writes_nothing(tmp_path, monkeypatch):
    """Required (b): halted KB → worker writes nothing."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=False)  # policy file deleted: halted
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    before = _row(cfg, fid)
    with pytest.raises(PolicyMissingError):
        _run_form_check(
            Store(cfg.db_path), cfg, fid,
            sheet_lock=threading.Lock(), read=_read_stub(),
        )
    after = _row(cfg, fid)
    assert before == after


def test_recheck_after_failure_fetches_from_lagging_watermark(
    tmp_path, monkeypatch
):
    """Required (c): re-check after failed check fetches from lagging watermark."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    exc = TokenRefreshError("token expired")
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"
    assert row["watermark"] is None  # old watermark (never advanced)
    # Re-check: the read stub should receive the same (None) watermark
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    stub = _read_stub(rows)
    _run_form_check(
        Store(cfg.db_path), cfg, fid, sheet_lock=threading.Lock(), read=stub
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] is None
    assert row["watermark"] is not None


def test_sequential_same_form_recheck_does_not_duplicate(
    tmp_path, monkeypatch
):
    """Required (d): a second check of the same form does not duplicate
    responses.

    This pins the watermark dedup at the worker level: after the first check
    records its watermark, a second check reads from the advanced watermark
    and finds no new rows (empty batch) rather than re-materialising the same
    responses. This test is sequential, not concurrent. The in-flight dedup in
    _start_form_check (the form_checking set / route 409) is covered by
    test_route_409_in_progress.
    """
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    lock = threading.Lock()
    # First check: materialises and records the watermark
    _run_form_check(
        Store(cfg.db_path), cfg, fid, sheet_lock=lock, read=_read_stub(rows)
    )
    row1 = _row(cfg, fid)
    assert row1["last_error_kind"] is None
    wm1 = row1["watermark"]
    assert wm1 is not None
    # Second check: reads from the advanced watermark → empty batch
    _run_form_check(
        Store(cfg.db_path), cfg, fid, sheet_lock=lock, read=_read_stub(rows)
    )
    row2 = _row(cfg, fid)
    # Watermark unchanged (no new rows to advance)
    assert row2["watermark"] == wm1
    # No duplicate source: only one source row exists
    with Store(cfg.db_path) as store:
        store.init_schema()
        sources = list(store._conn.execute(
            "SELECT * FROM sources WHERE path LIKE 'sources/form-%'"
        ))
        assert len(sources) == 1  # only one batch was materialised


def test_create_app_resume_does_nothing_on_halted_kb(
    tmp_path, monkeypatch
):
    """Required (e): create_app() resume does nothing on halted KB."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=False)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    from verinote.web import create_app
    create_app(cfg)
    time.sleep(0.2)
    row = _row(cfg, fid)
    assert row["last_checked_at"] is None
    assert row["last_error_kind"] is None


# --- contract tests ----------------------------------------------------------


def test_rotated_token_persisted_before_record_success(
    tmp_path, monkeypatch
):
    """Contract #2: success path — the rotated token is persisted BEFORE the
    check result is recorded (spy call order pins the ordering)."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    calls = []
    orig_save = webapp.save_google_grant

    def spy_save(grant):
        calls.append("save")
        orig_save(grant)

    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    monkeypatch.setattr(webapp, "save_google_grant", spy_save)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    store = Store(cfg.db_path)
    store.init_schema()
    orig_record = store.record_check_result

    def spy_record(*args, **kwargs):
        calls.append("record")
        return orig_record(*args, **kwargs)

    store.record_check_result = spy_record
    _run_form_check(
        store, cfg, fid,
        sheet_lock=threading.Lock(),
        read=_read_stub(rows, rotated=ROTATED_TOKEN),
    )
    # Contract #2: persist-first — the rotated token is saved strictly
    # before the check result is recorded
    assert calls.index("save") < calls.index("record")
    loaded = load_google_grant()
    assert loaded is not None
    assert loaded.refresh_token == ROTATED_TOKEN


def test_rotated_token_persisted_before_record_failure(
    tmp_path, monkeypatch
):
    """Contract #2: failure path — the live token carried by a failed refresh
    is persisted BEFORE the failed check is recorded (spy call order)."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    calls = []
    orig_save = webapp.save_google_grant

    def spy_save(grant):
        calls.append("save")
        orig_save(grant)

    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    monkeypatch.setattr(webapp, "save_google_grant", spy_save)
    store = Store(cfg.db_path)
    store.init_schema()
    orig_record = store.record_check_result

    def spy_record(*args, **kwargs):
        calls.append("record")
        return orig_record(*args, **kwargs)

    store.record_check_result = spy_record
    exc = TokenRefreshError("token expired", live_refresh_token=ROTATED_TOKEN)
    _run_form_check(
        store, cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    # Contract #2: persist-first — the live token is saved strictly before
    # the failed check is recorded
    assert calls.index("save") < calls.index("record")
    loaded = load_google_grant()
    assert loaded is not None
    assert loaded.refresh_token == ROTATED_TOKEN
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"


def test_404_records_sheet_not_found(tmp_path, monkeypatch):
    """404 → sheet_not_found writable in DB."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    exc = SheetReadError("not found", status=404, retryable=False)
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "sheet_not_found"


def test_429_backoff_sets_next_check_at(tmp_path, monkeypatch):
    """429 → next_check_at strictly after last_checked_at, error_kind=None."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    exc = SheetReadError("rate limited", status=429, retryable=True)
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] is None
    assert row["next_check_at"] is not None
    assert row["last_checked_at"] is not None
    assert row["next_check_at"] > row["last_checked_at"]


def test_403_records_sheet_forbidden(tmp_path, monkeypatch):
    """403 → sheet_forbidden (contract #5 mapping pinned at worker level)."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    exc = SheetReadError("forbidden", status=403, retryable=False)
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "sheet_forbidden"


def test_ingest_error_backs_off_without_error_kind(tmp_path, monkeypatch):
    """IngestError (collision guard) → failed check: OLD watermark kept,
    next_check_at backoff, error_kind=None (unmapped-exception discipline)."""
    from verinote.pipeline.ingest import IngestError

    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    exc = IngestError("collision")
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(exc=exc),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] is None
    assert row["last_checked_at"] is not None
    assert row["next_check_at"] is not None
    assert row["next_check_at"] > row["last_checked_at"]
    assert row["watermark"] is None  # old watermark (never advanced)


def test_two_forms_concurrent_reads_non_overlapping(
    tmp_path, monkeypatch
):
    """Contract #3: the sheet lock serialises concurrent checks on one grant;
    both forms complete healthy and the rotated token lands. (The
    persist-ORDER pin under real token rotation lives in
    test_two_forms_real_refresh_rotation_neither_expired.)"""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid1 = _form_kb(cfg, sheet_id="sheet_one")
    fid2 = _form_kb(cfg, sheet_id="sheet_two")
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    lock = threading.Lock()

    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )

    t1 = threading.Thread(
        target=lambda: _run_form_check(
            Store(cfg.db_path), cfg, fid1,
            sheet_lock=lock, read=_read_stub(rows, rotated=ROTATED_TOKEN),
        )
    )
    t2 = threading.Thread(
        target=lambda: _run_form_check(
            Store(cfg.db_path), cfg, fid2,
            sheet_lock=lock, read=_read_stub(rows),
        )
    )
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive()
    assert not t2.is_alive()
    # Both forms should have been checked
    row1 = _row(cfg, fid1)
    row2 = _row(cfg, fid2)
    assert row1["last_checked_at"] is not None
    assert row2["last_checked_at"] is not None
    # The rotated token from form 1 should have been persisted
    loaded = load_google_grant()
    assert loaded is not None
    assert loaded.refresh_token == ROTATED_TOKEN



def test_two_forms_real_refresh_rotation_neither_expired(tmp_path, monkeypatch):
    """Contract #3 (real refresh): two forms on ONE shared grant, each running
    the real read_sheet_values refresh against a mock OAuth endpoint that
    ROTATES (invalidates) the refresh token on every successful refresh.

    The grant load must sit INSIDE the sheet_lock critical section: the second
    form then captures the token the first form persisted and refreshes a LIVE
    token, so NEITHER form records token_expired. With the load outside the
    lock, both forms capture the same pre-rotation token and the loser
    refreshes a dead one (400 invalid_grant) — a spurious token_expired even
    though the persisted grant is healthy.
    """
    import json as _json
    import urllib.error
    import urllib.parse

    import verinote.google_sheets as gs
    from verinote.config import save_google_grant

    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid1 = _form_kb(cfg, sheet_id="sheet_one")
    fid2 = _form_kb(cfg, sheet_id="sheet_two")
    # ONE shared grant: REFRESH_TOKEN is the only live refresh token at start
    save_google_grant(_grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    # Keep the load real (the in-lock position is what is under test) but widen
    # the capture->refresh window: with the load outside the lock this forces
    # both forms to capture the same pre-rotation token before either refreshes,
    # so the race is deterministic rather than scheduling luck. In the fixed
    # code this sleep sits under the lock and is harmless.
    orig_load = webapp.load_google_grant

    def _slow_load():
        grant = orig_load()
        time.sleep(0.15)
        return grant

    monkeypatch.setattr(webapp, "load_google_grant", _slow_load)

    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    state_lock = threading.Lock()
    state = {"n": 1, "live": REFRESH_TOKEN}
    issued: list[str] = []

    class _Resp:
        def __init__(self, data: bytes):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return self.data

    def _fake_open(req, *, timeout):
        if "token" in req.full_url:
            presented = urllib.parse.parse_qs(
                req.data.decode("utf-8")
            ).get("refresh_token", [None])[0]
            with state_lock:
                if presented != state["live"]:
                    # Google invalidates the previous refresh token on
                    # rotation: a stale capture is a dead token.
                    raise urllib.error.HTTPError(
                        req.full_url, 400, "invalid_grant", None, None
                    )
                state["n"] += 1
                state["live"] = f"synthetic-rotated-token-{state['n']}"
                issued.append(state["live"])
            payload = _json.dumps(
                {
                    "access_token": "synthetic-access",
                    "expires_in": 3600,
                    "refresh_token": state["live"],
                }
            ).encode()
            return _Resp(payload)
        payload = _json.dumps({"values": [list(r) for r in rows]}).encode()
        return _Resp(payload)

    monkeypatch.setattr(gs, "_open", _fake_open)
    monkeypatch.setattr(gs, "_sleep", lambda s: None)

    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _check(fid):
        barrier.wait()
        _run_form_check(
            Store(cfg.db_path), cfg, fid, sheet_lock=lock,
            # NO read= stub: the real read_sheet_values refreshes through
            # _fake_open
        )

    t1 = threading.Thread(target=_check, args=(fid1,))
    t2 = threading.Thread(target=_check, args=(fid2,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive()
    assert not t2.is_alive()
    # NEITHER form records token_expired (the race's fingerprint)
    row1 = _row(cfg, fid1)
    row2 = _row(cfg, fid2)
    assert row1["last_error_kind"] is None, f"form 1: {row1}"
    assert row2["last_error_kind"] is None, f"form 2: {row2}"
    assert row1["last_checked_at"] is not None
    assert row2["last_checked_at"] is not None
    # Both batches materialised (one source per sheet)
    with Store(cfg.db_path) as store:
        store.init_schema()
        sources = list(store._conn.execute(
            "SELECT * FROM sources WHERE path LIKE 'sources/form-%'"
        ))
    assert len(sources) == 2
    # Two LIVE refreshes, each building on the previous (T1 -> T2 -> T3);
    # the persisted grant is the newest one
    assert len(issued) == 2
    loaded = load_google_grant()
    assert loaded is not None
    assert loaded.refresh_token == issued[-1]

# --- error mapping tests -----------------------------------------------------


def test_no_grant_records_token_expired(tmp_path, monkeypatch):
    """Missing grant → token_expired, materialize not called."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: None)
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    stub = _read_stub()
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=stub,
    )
    assert len(stub.calls) == 0  # materialize was NOT called
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"


def test_corrupt_grant_records_token_expired(tmp_path, monkeypatch):
    """Corrupt grant → token_expired."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)

    def _corrupt():
        raise GoogleOAuthCorruptError("corrupt")

    monkeypatch.setattr(webapp, "load_google_grant", _corrupt)
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"


def test_no_client_id_records_token_expired(tmp_path, monkeypatch):
    """Grant present, client_id=None → token_expired, materialize not called."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: None)
    stub = _read_stub()
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=stub,
    )
    assert len(stub.calls) == 0
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"


def test_corrupt_client_id_records_token_expired(tmp_path, monkeypatch):
    """Grant present, client_id corrupt → token_expired."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())

    def _corrupt_client():
        raise GoogleOAuthCorruptError("corrupt client_id")

    monkeypatch.setattr(webapp, "load_google_client_id", _corrupt_client)
    _run_form_check(
        Store(cfg.db_path), cfg, fid,
        sheet_lock=threading.Lock(), read=_read_stub(),
    )
    row = _row(cfg, fid)
    assert row["last_error_kind"] == "token_expired"


# --- route tests -------------------------------------------------------------


def test_route_404_unknown_form(tmp_path):
    """Route 404 for unknown form_id."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    from fastapi.testclient import TestClient
    from verinote.web import create_app
    client = TestClient(create_app(cfg))
    resp = client.post("/integrations/forms/9999/check")
    assert resp.status_code == 404


def test_route_409_in_progress(tmp_path, monkeypatch):
    """Route 409 for in-progress check.

    The form_checking set is the in-flight dedup: if a check is already running,
    the route must return 409 rather than starting a second one.
    """
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    _stub_google_transport(monkeypatch)
    from fastapi.testclient import TestClient
    from verinote.web import create_app
    app = create_app(cfg)
    # Wait for the resume check to finish (fid leaves form_checking)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and fid in app.state.form_checking:
        time.sleep(0.01)
    # Now simulate an in-flight check by adding fid to the set
    app.state.form_checking.add(fid)
    try:
        client = TestClient(app)
        resp = client.post(f"/integrations/forms/{fid}/check")
        assert resp.status_code == 409
    finally:
        app.state.form_checking.discard(fid)


def test_route_202_started(tmp_path, monkeypatch):
    """Route 202 with {started:True, form_id}."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    _stub_google_transport(monkeypatch, rows)
    from fastapi.testclient import TestClient
    from verinote.web import create_app
    app = create_app(cfg)
    # Wait for the resume check to finish (it started one for this form)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and fid in app.state.form_checking:
        time.sleep(0.01)
    # The resume check already recorded a result; reset for a clean 202 test
    with Store(cfg.db_path) as store:
        store.init_schema()
        store.record_check_result(
            fid, watermark=None, last_checked_at=None,
            next_check_at=None, error_kind=None,
        )
    client = TestClient(app)
    resp = client.post(f"/integrations/forms/{fid}/check")
    assert resp.status_code == 202
    data = resp.json()
    assert data["started"] is True
    assert data["form_id"] == fid
    # Join the POST-triggered worker before returning: once the test ends,
    # monkeypatch undo restores the REAL loaders/transport, so an un-joined
    # worker that straddles that boundary would run the real credential path
    # (base suite must never dial Google, even by winning a scheduler race).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and fid in app.state.form_checking:
        time.sleep(0.01)
    assert fid not in app.state.form_checking


# --- resume tests -------------------------------------------------------------


def test_resume_healthy_kb_starts_check(tmp_path, monkeypatch):
    """Healthy KB resume starts check for enabled forms."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    _stub_google_transport(monkeypatch, rows)
    from verinote.web import create_app
    create_app(cfg)
    _wait_for(
        lambda: _row(cfg, fid)["last_checked_at"] is not None,
        timeout=5.0,
    )


def test_resume_skips_backoff_form(tmp_path, monkeypatch):
    """Form with future next_check_at is NOT re-checked on startup."""
    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    # Set next_check_at to the future
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with Store(cfg.db_path) as store:
        store.init_schema()
        store.record_check_result(
            fid, watermark=None, last_checked_at="2026-01-01 00:00:00",
            next_check_at=future, error_kind=None,
        )
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)
    from verinote.web import create_app
    create_app(cfg)
    time.sleep(0.3)
    row = _row(cfg, fid)
    # last_checked_at should NOT have changed (still the old value)
    assert row["last_checked_at"] == "2026-01-01 00:00:00"


def test_real_read_path_materializes_source(tmp_path, monkeypatch):
    """F-3 regression: the default (non-stub) read path must actually
    materialise a source and job. This test drives _run_form_check without
    a read= stub (so read_sheet_values is used) but scripts the transport
    seam (_open) to return synthetic data without hitting the network."""
    import json as _json


    cfg = _cfg(tmp_path)
    _policy_kb(cfg, with_policy=True)
    fid = _form_kb(cfg)
    monkeypatch.setattr(webapp, "load_google_grant", lambda: _grant())
    monkeypatch.setattr(webapp, "load_google_client_id", lambda: CLIENT_ID)

    rows = (
        HEADER,
        ("2026-09-27 10:00", "Sample Answer A", "Sample Answer B"),
    )
    call_count = []

    class _Resp:
        def __init__(self, data: bytes):
            self.data = data
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.data

    def _fake_open(req, *, timeout):
        call_count.append(req.full_url)
        if "token" in req.full_url:
            # Token refresh endpoint: return a synthetic access token
            payload = _json.dumps({"access_token": "synthetic-access", "expires_in": 3600}).encode()
            return _Resp(payload)
        else:
            # Sheet read endpoint: return synthetic rows
            payload = _json.dumps({"values": [list(r) for r in rows]}).encode()
            return _Resp(payload)

    import verinote.google_sheets as gs
    monkeypatch.setattr(gs, "_open", _fake_open)
    monkeypatch.setattr(gs, "_sleep", lambda s: None)

    lock = threading.Lock()
    _run_form_check(
        Store(cfg.db_path), cfg, fid, sheet_lock=lock,
        # NO read= stub: the real read_sheet_values is used
    )
    row = _row(cfg, fid)
    # The check must have SUCCEEDED (error_kind=None), not failed
    assert row["last_error_kind"] is None, f"check failed: {row}"
    assert row["watermark"] is not None
    # A source and job must have been materialised
    with Store(cfg.db_path) as store:
        store.init_schema()
        sources = list(store._conn.execute(
            "SELECT * FROM sources WHERE path LIKE 'sources/form-%'"
        ))
        assert len(sources) == 1
        jobs = list(store._conn.execute("SELECT * FROM extraction_jobs"))
        assert len(jobs) == 1
