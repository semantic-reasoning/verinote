# SPDX-License-Identifier: MPL-2.0
"""Durable question-repair lifecycle coverage."""

import pytest

from verinote.llm.base import LLMError, MAX_REASON_LENGTH
from verinote.pipeline.repair import process_repair_job
from verinote.pipeline.policy_state import POLICY_RELPATH, assert_writable, write_default_policy
from verinote.store import Store


class _OutageClient:
    def __init__(self):
        self.calls = 0

    def extract_query_intent(self, *, question, schema_hint):
        self.calls += 1
        raise LLMError("synthetic provider outage")


class _NoCallClient:
    def extract_query_intent(self, *, question, schema_hint):
        raise AssertionError("skipped question must not reach the provider")


def _store(tmp_path):
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _review_question(store, text):
    qid = store.add_question(text)
    store.set_question_query(qid, 'review_required("synthetic")', "review_required")
    return qid


def test_enqueue_repair_job_snapshots_and_reuses_live_job(tmp_path):
    store = _store(tmp_path)
    first = _review_question(store, "What is synthetic one?")
    second = _review_question(store, "What is synthetic two?")

    job, created = store.enqueue_repair_job(provider="fake", model="m")
    duplicate, duplicate_created = store.enqueue_repair_job(provider="fake", model="m")

    assert created is True
    assert duplicate_created is False
    assert int(duplicate["id"]) == int(job["id"])
    assert [int(item["question_id"]) for item in store.repair_job_items(int(job["id"]))] == [
        first,
        second,
    ]


def test_repair_resume_never_adopts_a_live_lease(tmp_path):
    store = _store(tmp_path)
    _review_question(store, "What is synthetic?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    job_id = int(job["id"])

    assert store.claim_repair_job(job_id, "live-owner")
    assert store.repair_jobs_to_resume() == []

    store._conn.execute(
        "UPDATE repair_jobs SET lease_until = datetime('now', '-1 second') WHERE id = ?",
        (job_id,),
    )
    assert [int(row["id"]) for row in store.repair_jobs_to_resume()] == [job_id]


def test_expired_item_is_reclaimed_and_stale_owner_is_fenced(tmp_path):
    old = _store(tmp_path)
    new = Store(tmp_path / "kb.sqlite")
    new.init_schema()
    qid = _review_question(old, "What is synthetic?")
    job, _ = old.enqueue_repair_job(provider="fake", model="m")
    job_id = int(job["id"])

    assert old.claim_repair_job(job_id, "old")
    old_item = old.claim_next_repair_item(job_id, "old")
    new._conn.execute(
        "UPDATE repair_jobs SET lease_until = datetime('now', '-1 second') WHERE id = ?",
        (job_id,),
    )
    assert old.renew_repair_job_lease(job_id, "old") is False
    assert new.claim_repair_job(job_id, "new")

    assert old.finish_repair_item(int(old_item["id"]), "old", status="done") is False
    assert old.persist_repair_question(
        job_id, int(old_item["id"]), "old", qid, "old write", "translated", ""
    ) is False
    from verinote.pipeline.query import write_query_file

    assert write_query_file(
        old,
        tmp_path,
        publication_guard=lambda conn: Store.repair_query_publication_owned(conn, job_id, "old"),
    ) is None
    assert old.repair_job_question(qid)["status"] == "review_required"
    reclaimed = new.claim_next_repair_item(job_id, "new")
    assert int(reclaimed["id"]) == int(old_item["id"])
    assert reclaimed["owner_token"] == "new"
    assert write_query_file(
        new,
        tmp_path,
        publication_guard=lambda conn: Store.repair_query_publication_owned(conn, job_id, "new"),
    ) == tmp_path / "facts" / "query.dl"


def test_policy_deleted_during_provider_call_leaves_job_recoverable(tmp_path):
    store = _store(tmp_path)
    qid = _review_question(store, "What is synthetic?")
    write_default_policy(store, tmp_path, origin="scaffold")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")

    class DeletingClient:
        def extract_query_intent(self, *, question, schema_hint):
            (tmp_path / POLICY_RELPATH).unlink()
            raise LLMError("synthetic provider outage")

    try:
        process_repair_job(
            store, DeletingClient(), job_id=int(job["id"]), root=tmp_path,
            policy_guard=lambda: assert_writable(store),
        )
    except Exception as exc:
        assert "policy" in str(exc).lower()
    else:
        raise AssertionError("deleted policy must stop the worker before persistence")

    assert store.repair_job_question(qid)["status"] == "review_required"
    assert store.get_repair_job(int(job["id"]))["status"] == "running"
    assert store.repair_job_items(int(job["id"]))[0]["status"] == "running"


def test_query_file_failure_is_retried_without_recalling_completed_question(
    tmp_path, monkeypatch, fake_client, intent_payload,
):
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    _review_question(store, "Where was Sample Person born?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    client = fake_client(intent=intent_payload("lookup_object", subject="Sample Person", relation="born_in"))
    original_writer = repair.write_query_file
    monkeypatch.setattr(repair, "write_query_file", lambda store, root: (_ for _ in ()).throw(OSError("synthetic disk full")))

    process_repair_job(store, client, job_id=int(job["id"]), root=tmp_path)

    assert store.get_repair_job(int(job["id"]))["status"] == "pending"
    assert store.repair_job_items(int(job["id"]))[0]["status"] == "done"
    monkeypatch.setattr(repair, "write_query_file", original_writer)
    process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)
    assert store.get_repair_job(int(job["id"]))["status"] == "done"


def test_provider_failure_stops_after_first_snapshot_item(tmp_path):
    store = _store(tmp_path)
    _review_question(store, "What is synthetic one?")
    _review_question(store, "What is synthetic two?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    client = _OutageClient()

    process_repair_job(store, client, job_id=int(job["id"]), root=tmp_path)

    assert client.calls == 1
    saved = store.get_repair_job(int(job["id"]))
    assert saved["status"] == "failed"
    assert [item["status"] for item in store.repair_job_items(int(job["id"]))] == [
        "failed",
        "pending",
    ]


def test_repair_job_skips_deleted_and_no_longer_review_question(tmp_path):
    store = _store(tmp_path)
    deleted = _review_question(store, "What is deleted?")
    changed = _review_question(store, "What is changed?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    store.delete_question(deleted)
    store.set_question_query(changed, None, "no_answer", "already resolved")

    process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)

    saved = store.get_repair_job(int(job["id"]))
    assert saved["status"] == "done"
    assert int(saved["skipped_items"]) == 2
    assert [item["status"] for item in store.repair_job_items(int(job["id"]))] == [
        "skipped",
        "skipped",
    ]


def test_repair_job_processes_the_enqueued_snapshot(tmp_path, fake_client, intent_payload):
    store = _store(tmp_path)
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    first = _review_question(store, "Where was Sample Person born?")
    second = _review_question(store, "Where was Sample Person born again?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    client = fake_client(
        intent=intent_payload("lookup_object", subject="Sample Person", relation="born_in")
    )

    process_repair_job(store, client, job_id=int(job["id"]), root=tmp_path)

    assert store.get_repair_job(int(job["id"]))["status"] == "done"
    assert [item["status"] for item in store.repair_job_items(int(job["id"]))] == [
        "done",
        "done",
    ]
    assert [store.repair_job_question(qid)["status"] for qid in (first, second)] == [
        "translated",
        "translated",
    ]


def test_blank_exception_in_the_item_prepare_path_names_the_type(tmp_path, monkeypatch):
    """#579, item-prepare site: a blank ``str(exc)`` must name the exception's
    type, not leave the job row reading "Repair failed: " and the failed item's
    reason blank.

    Drives the REAL ``process_repair_job`` (not a monkeypatched stand-in) by
    raising an argument-less ``ValueError()`` from ``_prepare_repair_question``.
    The two publish-path sites are untouched, so reverting only this site
    reddens this test and the publish-path tests stay green.
    """
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    _review_question(store, "What is synthetic?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")

    def blank(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(repair, "_prepare_repair_question", blank)

    with pytest.raises(ValueError):
        process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)

    saved = store.get_repair_job(int(job["id"]))
    assert saved["message"] == "Repair failed: ValueError"
    assert store.repair_job_items(int(job["id"]))[0]["status"] == "failed"
    assert store.repair_job_items(int(job["id"]))[0]["reason"] == "ValueError"


def test_item_prepare_path_collapses_internal_whitespace_in_the_reason(
    tmp_path, monkeypatch
):
    """#584, AC-3 (collapse half). The item-prepare `except Exception` clause
    folds every internal whitespace run of the cause to one space before it
    writes the failed item's reason and the job message. Drive it with a cause
    whose collapsed form stays UNDER `MAX_REASON_LENGTH`, so the cap is the
    identity and this test isolates the collapse: 100 `a`s, an irregular
    whitespace run that must fold to one space, then 50 `b`s. Dropping the
    `.split()` collapse leaves the raw run in the durable reason and reddens
    ONLY this test; dropping the `[:MAX_REASON_LENGTH]` bound (a no-op on a
    sub-cap message) stays green.
    """
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    _review_question(store, "What is synthetic?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    cause = "a" * 100 + "  \n\t  " + "b" * 50
    expected = "a" * 100 + " " + "b" * 50
    assert len(expected) < MAX_REASON_LENGTH  # the cap must be the identity here

    def boom(*args, **kwargs):
        raise ValueError(cause)

    monkeypatch.setattr(repair, "_prepare_repair_question", boom)

    with pytest.raises(ValueError):
        process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)

    assert store.repair_job_items(int(job["id"]))[0]["reason"] == expected
    assert store.get_repair_job(int(job["id"]))["message"] == "Repair failed: " + expected


def test_item_prepare_path_caps_the_reason_at_the_reason_cap(tmp_path, monkeypatch):
    """#584, AC-3 (cap half) + the issue's sentinel probe: this clause's reason
    must reach an assertion. Drive it with a single unbroken token carrying NO
    internal whitespace, so the collapse is the identity and this test isolates
    the bound: `MAX_REASON_LENGTH + 50` `c`s, which must land on the failed
    item's reason as exactly `MAX_REASON_LENGTH`. Dropping
    `[:MAX_REASON_LENGTH]` reddens ONLY this test (the sentinel); dropping the
    `.split()` collapse (a no-op on a whitespace-free token) stays green.
    """
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    _review_question(store, "What is synthetic?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    cause = "c" * (MAX_REASON_LENGTH + 50)
    expected = "c" * MAX_REASON_LENGTH
    assert len(cause) > MAX_REASON_LENGTH

    def boom(*args, **kwargs):
        raise ValueError(cause)

    monkeypatch.setattr(repair, "_prepare_repair_question", boom)

    with pytest.raises(ValueError):
        process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)

    assert store.repair_job_items(int(job["id"]))[0]["reason"] == expected
    assert store.get_repair_job(int(job["id"]))["message"] == "Repair failed: " + expected


def test_blank_exception_in_the_completion_publish_path_names_the_type(
    tmp_path, monkeypatch, fake_client, intent_payload,
):
    """#579, completion-publish site: after an item completes, a blank
    ``str(exc)`` from the derived writer must name the exception's type, not
    leave "Query draft regeneration pending: " dangling.

    This is the publish reached on the same iteration an item is finished,
    distinct from the no-item publish site. Reverting only this site reddens
    this test; the no-item publish test stays green.
    """
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    _review_question(store, "Where was Sample Person born?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    client = fake_client(
        intent=intent_payload("lookup_object", subject="Sample Person", relation="born_in")
    )

    def blank_writer(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(repair, "write_query_file", blank_writer)

    process_repair_job(store, client, job_id=int(job["id"]), root=tmp_path)

    assert store.repair_job_items(int(job["id"]))[0]["status"] == "done"
    saved = store.get_repair_job(int(job["id"]))
    assert saved["status"] == "pending"
    assert saved["message"] == "Query draft regeneration pending: ValueError"


def test_blank_exception_in_the_no_item_publish_path_names_the_type(
    tmp_path, monkeypatch, fake_client, intent_payload,
):
    """#579, no-item publish site: with every item already done, a blank
    ``str(exc)`` from the derived writer must name the exception's type, not
    leave "Query draft regeneration pending: " dangling.

    Run 1 completes the item (deferred by a failed publish); run 2 re-claims
    the job, finds no item to process, and reaches the no-item publish site.
    The final assertion is on run 2, so reverting only this site reddens this
    test while the completion-publish test stays green.
    """
    import verinote.pipeline.repair as repair

    store = _store(tmp_path)
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    _review_question(store, "Where was Sample Person born?")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")
    client = fake_client(
        intent=intent_payload("lookup_object", subject="Sample Person", relation="born_in")
    )

    def blank_writer(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(repair, "write_query_file", blank_writer)

    # Run 1: the item completes (done); the failed publish defers the job.
    process_repair_job(store, client, job_id=int(job["id"]), root=tmp_path)
    assert store.repair_job_items(int(job["id"]))[0]["status"] == "done"
    assert store.get_repair_job(int(job["id"]))["status"] == "pending"

    # Run 2: no item left to process -> the no-item publish site.
    process_repair_job(store, _NoCallClient(), job_id=int(job["id"]), root=tmp_path)
    saved = store.get_repair_job(int(job["id"]))
    assert saved["status"] == "pending"
    assert saved["message"] == "Query draft regeneration pending: ValueError"
