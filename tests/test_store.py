# SPDX-License-Identifier: MPL-2.0
from verinote.engine import compile_dl
from verinote.store import ENGINE_STATUSES, Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_toggle_promotes_and_reverts(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)

    row = s.toggle_review(fid)
    assert row["status"] == "confirmed"

    row = s.toggle_review(fid)
    assert row["status"] == "needs_review"


def test_review_queue_excludes_confirmed(tmp_path):
    s = _store(tmp_path)
    s.add_fact("A", "r", "B", status="needs_review")
    s.add_fact("C", "r", "D", status="confirmed")
    queue = s.review_queue()
    assert [q["subject"] for q in queue] == ["A"]


def test_review_log_records_decision(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    rows = list(s._conn.execute("SELECT action FROM review_log"))
    assert rows[0]["action"] == "toggled"


def test_compile_dl_only_projects_triples(tmp_path):
    s = _store(tmp_path)
    # non-ASCII placeholder keeps UTF-8 round-trip coverage (not a real entity)
    s.add_fact("예시기관", "is_a", "참여기관", status="confirmed")
    s.add_fact("x", "y", "z", status="needs_review")  # must NOT appear
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert 'relation("예시기관", "is_a", "참여기관").' in dl
    assert "needs_review" not in dl
    assert '"x"' not in dl


def test_compile_dl_escapes_quotes(tmp_path):
    s = _store(tmp_path)
    s.add_fact('a"b', "r", "c", status="confirmed")
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert r'relation("a\"b", "r", "c").' in dl


def test_amend_fact_persists_and_audits(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", note="orig")
    after = s.amend_fact(fid, subject="A2", relation="became", obj="C", note="fixed")
    assert (after["subject"], after["relation"], after["object"], after["note"]) == (
        "A2",
        "became",
        "C",
        "fixed",
    )
    assert [e["action"] for e in s.fact_log(fid)] == ["amended"]


def test_amend_missing_fact_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.amend_fact(999, subject="x", relation="y", obj="z") is None


def test_delete_source_removes_source_facts_and_terms(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    source_fact = s.add_fact("A", "r", "B", status="candidate", source_id=sid)
    unrelated_fact = s.add_fact("C", "r", "D", status="candidate")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["body"])

    deleted = s.delete_source(sid)

    assert deleted is not None
    assert deleted["path"] == "sources/a.txt"
    assert s.sources() == []
    assert [f["id"] for f in s.facts()] == [unrelated_fact]
    assert s.get_fact_terms(source_fact) is None
    assert s.get_fact_terms(unrelated_fact) is not None
    assert s.get_extraction_job(job_id) is None
    assert s.source_chunks(job_id) == []


def test_delete_missing_source_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.delete_source(999) is None


def test_extraction_job_tracks_chunk_progress_and_retry(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid,
        provider="fake",
        model="m",
        total_chunks=2,
        message="queued",
    )
    chunk_ids = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a", "b"])

    s.mark_extraction_job_running(job_id)
    assert s.mark_chunk_running(chunk_ids[0])["attempts"] == 1
    s.mark_chunk_done(chunk_ids[0], candidates=2)
    s.mark_chunk_running(chunk_ids[1])
    s.mark_chunk_failed(chunk_ids[1], "provider down")

    job = s.get_extraction_job(job_id)
    assert job["status"] == "failed"
    assert job["completed_chunks"] == 1
    assert job["failed_chunks"] == 1
    assert job["candidate_count"] == 2
    assert "1 chunk(s) failed" in job["message"]

    assert s.retry_failed_chunks(job_id) == 1
    assert s.source_chunks(job_id)[1]["status"] == "pending"


def test_reset_running_chunks_makes_job_resumable(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]

    s.mark_extraction_job_running(job_id)
    s.mark_chunk_running(chunk_id)

    assert s.reset_running_chunks(job_id) == 1
    assert s.next_pending_chunk(job_id)["id"] == chunk_id
    assert s.get_extraction_job(job_id)["status"] == "pending"


def test_mark_chunk_running_returns_none_when_chunk_already_claimed(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/a.txt")
    job_id = s.create_extraction_job(
        source_id=sid, provider="fake", model="m", total_chunks=1
    )
    chunk_id = s.add_source_chunks(job_id=job_id, source_id=sid, chunks=["a"])[0]

    assert s.mark_chunk_running(chunk_id) is not None
    assert s.mark_chunk_running(chunk_id) is None


def test_fact_log_orders_decisions(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    s.amend_fact(fid, subject="A", relation="r", obj="B2")
    assert [e["action"] for e in s.fact_log(fid)] == ["toggled", "amended"]


def test_questions_add_list_and_translate(tmp_path):
    s = _store(tmp_path)
    qid = s.add_question("Where was Ada born?")
    assert [(q["id"], q["status"]) for q in s.questions()] == [(qid, "pending")]
    assert [q["id"] for q in s.questions(pending_only=True)] == [qid]

    s.set_question_query(qid, ".decl answer_q1(value: symbol)", "translated")
    assert s.questions(pending_only=True) == []
    assert s.questions()[0]["status"] == "translated"

    s.delete_question(qid)
    assert s.questions() == []
