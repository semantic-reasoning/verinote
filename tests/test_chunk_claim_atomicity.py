# SPDX-License-Identifier: MPL-2.0
"""The chunk claim is one statement: the CAS and its row arrive together (#489).

`Store.mark_chunk_running` used to be an `UPDATE` and then a separate
`get_source_chunk`. The connection is autocommit, so the claim was on disk before
the second statement ran, and anything that happened in between happened to a
chunk that was already `running` under a caller holding nothing. These tests pin
the collapsed form from both sides of that gap: a read-back that cannot run at
all, and a peer that arrives while the caller is between statements.

THE SQLITE FLOOR IS ALREADY PAID FOR. `RETURNING` arrived in SQLite 3.35.0, and
nothing added here gates on a version. It does not have to: this suite already
runs `ALTER TABLE ... DROP COLUMN` (`test_ingest_unreadable_chars.py`,
`test_cli.py`), which arrived in that same release, and there is no
`sqlite_version` check anywhere under `verinote/` or `tests/` that could skip
past either one. An interpreter these tests pass on has `RETURNING`.

WHY THE GUARDS ARE SHAPED LIKE THIS. There is no injection point left inside the
fixed method — that is the point of the fix — so the tests aimed at the old second
statement either arrange the condition that *would* have hit it and assert the
outcome is unaffected, or pin the statement count directly. Not every test here
is aimed at that mutant, though, and which one each answers to is worth stating
rather than blurring:

* T1, T2 and T3 go red when the two statements are put back. T2b and T3a stay
  green against that mutant, by design — neither is about the read-back.
* T2b goes red against a mutant that keeps `RETURNING` but restores the
  `rowcount != 1` guard in front of the fetch.
* T3a goes red against a mutation that stops the matcher matching: a typo in
  `READ_BACK_PREFIX`, or dropping the `.upper()` that normalises the SQL against
  it — both measured, both with the same signature. Those disarm T3 while leaving
  it, and everything else here, green, which is what T3a is for.

The ordering notes below are what keeps each of them from passing for the wrong
reason.
"""

import sqlite3

import pytest

from verinote.store.db import Store

# The read-back the two-statement claim used to issue, as the interceptors below
# match it: `get_source_chunk` is `SELECT * FROM source_chunks WHERE id = ?`
# (`store/db.py`). Shared by the injectors and by their self-checks on purpose —
# a typo here must not be able to disarm one while leaving the other looking
# armed.
READ_BACK_PREFIX = "SELECT * FROM SOURCE_CHUNKS"


class SelectsFail:
    """A connection whose every SELECT raises; writes go through untouched.

    STATELESS ON PURPOSE — it fires again and again rather than once. Measured
    against a mutant that restores the two statements, a one-shot injector plus a
    positive control placed before the claim leaves T1 green; the injector's shape
    and the control's placement are each enough on their own to keep it red, and
    both are kept.
    """

    def __init__(self, conn):
        self._conn = conn
        self.fired = 0

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            self.fired += 1
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class OnReadBack:
    """Run `side_effect` once, the first time the read-back SELECT is issued."""

    def __init__(self, conn, side_effect):
        self._conn = conn
        self._side_effect = side_effect
        self.fired = False

    def execute(self, sql, params=()):
        if not self.fired and sql.lstrip().upper().startswith(READ_BACK_PREFIX):
            self.fired = True
            self._side_effect()
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _kb(path):
    store = Store(path / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("sources/a.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id, source_id=source_id, chunks=["alpha"]
    )[0]
    return store, job_id, chunk_id


def test_the_claim_survives_a_connection_that_cannot_read_the_chunk_back(tmp_path):
    """T1. No SELECT can succeed, and the claim still comes back with its row."""
    store, _job_id, chunk_id = _kb(tmp_path)
    real = store._conn
    store._conn = SelectsFail(real)
    try:
        claimed = store.mark_chunk_running(chunk_id)

        # POSITIVE CONTROL, AND ITS PLACEMENT IS LOAD-BEARING. It proves the
        # injector was armed for the whole of the call above rather than
        # asserting anything about the claim. Run it *before* the claim and a
        # one-shot injector would already be spent, so the two-statement mutant
        # would sail through this test as well — the control has to come after.
        with pytest.raises(sqlite3.OperationalError):
            store.get_source_chunk(chunk_id)
    finally:
        store._conn = real

    assert claimed is not None
    assert claimed["id"] == chunk_id
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    # and the caller's handle agrees with the disk, read on a healthy connection
    assert dict(store.get_source_chunk(chunk_id)) == dict(claimed)


def test_the_claim_is_a_single_statement_carrying_the_whole_post_image(tmp_path):
    """T2. One statement touches `source_chunks`, and it returns the new row."""
    store, _job_id, chunk_id = _kb(tmp_path)
    before = store.get_source_chunk(chunk_id)

    seen = []
    store._conn.set_trace_callback(seen.append)
    try:
        claimed = store.mark_chunk_running(chunk_id)
    finally:
        store._conn.set_trace_callback(None)

    touching = [sql for sql in seen if "source_chunks" in sql]
    assert len(touching) == 1, touching
    # the one statement is the CAS, and it is the one carrying the row back
    assert touching[0].lstrip().upper().startswith("UPDATE")
    assert "RETURNING" in touching[0].upper()
    # `RETURNING *` has to be the same shape the callers used to get from
    # `get_source_chunk`, post-image values and all.
    assert list(claimed.keys()) == list(before.keys())
    assert claimed["status"] == "running"
    assert claimed["attempts"] == before["attempts"] + 1
    assert claimed["error"] == ""
    assert claimed["text"] == "alpha"


def test_a_claim_that_matches_nothing_returns_none_and_writes_nothing(tmp_path):
    """T2b. The missing row is the whole verdict — no `rowcount` reading it."""
    store, _job_id, chunk_id = _kb(tmp_path)
    assert store.mark_chunk_running(chunk_id) is not None

    assert store.mark_chunk_running(chunk_id) is None  # already held
    assert store.mark_chunk_running(999999) is None  # no such chunk

    held = store.get_source_chunk(chunk_id)
    assert held["status"] == "running"
    assert held["attempts"] == 1  # the declined claim did not spend an attempt


def test_the_matcher_the_peer_test_relies_on_really_fires_on_a_read_back(tmp_path):
    """T3a. Self-check for T3's injector: a typo in it would disarm T3 silently.

    T3 below detects the peer by intercepting the read-back SELECT. If
    `READ_BACK_PREFIX` stops matching what `get_source_chunk` issues, that
    interceptor fires for nobody and T3 goes green on both the fixed code and the
    two-statement mutant — a dead guard with no red anywhere. This asserts the
    matcher against a call that exists in either version, so the typo has
    somewhere to show up.
    """
    store, _job_id, chunk_id = _kb(tmp_path)
    real = store._conn
    interceptor = OnReadBack(real, lambda: None)
    store._conn = interceptor
    try:
        store.get_source_chunk(chunk_id)
    finally:
        store._conn = real

    assert interceptor.fired is True


def test_the_claim_hands_back_its_own_attempt_count_not_a_peers(tmp_path):
    """T3. A peer that reclaims the chunk cannot be mistaken for the caller.

    `requeue_chunk_claim` releases a claim only while the row still carries the
    attempt count the caller's own claim put there (`store/db.py`). With the
    read-back as a separate statement, a peer rewinding and re-taking the chunk
    in between made that read-back return the PEER's count, and the caller then
    released a claim that was not its own. Collapsed into one statement, the
    count the caller holds is the one its own CAS wrote.

    Note what is deliberately NOT asserted: that the interceptor fired during the
    claim. On the fixed code it never can, because there is no read-back to fire
    on — so `fired` is the wrong thing to pin here, and T3a pins the matcher
    instead. What is asserted is the outcome the caller sees either way.
    """
    store, job_id, chunk_id = _kb(tmp_path)
    peer = Store(tmp_path / "kb.sqlite")

    def peer_reclaims():
        # what a second process's resume loop amounts to: rewind the stray
        # `running` chunk with the job, then take it afresh
        peer.rollback_extraction_job(job_id, "rewound by the resume loop")
        peer.mark_chunk_running(chunk_id)

    real = store._conn
    interceptor = OnReadBack(real, peer_reclaims)
    store._conn = interceptor
    try:
        claimed = store.mark_chunk_running(chunk_id)
    finally:
        store._conn = real
    if not interceptor.fired:
        # No read-back existed to land inside, so the peer lands immediately
        # after instead — the same peer at the same logical moment, which is what
        # makes this comparable across both versions of the claim.
        peer_reclaims()

    assert claimed["attempts"] == 1  # ours, not the peer's second attempt
    assert store.requeue_chunk_claim(chunk_id, attempts=int(claimed["attempts"])) is False

    still_the_peers = store.get_source_chunk(chunk_id)
    assert still_the_peers["status"] == "running"
    assert still_the_peers["attempts"] == 2
    peer.close()
