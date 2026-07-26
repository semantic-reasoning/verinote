# SPDX-License-Identifier: MPL-2.0
"""Durable question-repair lifecycle coverage."""

from verinote.llm.base import LLMError
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
