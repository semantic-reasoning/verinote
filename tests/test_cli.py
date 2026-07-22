# SPDX-License-Identifier: MPL-2.0
import sqlite3
import sys
from pathlib import Path

import pytest

import verinote.cli as cli
from verinote import config
from verinote.engine import DEFAULT_POLICY
from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.ingest import register_converter
from verinote.pipeline.query_intent import parse_query_intent
from verinote.store import Store, engine_statuses
from verinote.store.fact_input import structural_term


def _env(monkeypatch, tmp_path):
    """Point a fresh KB at tmp_path; `cli.main` reads these via Config.load()."""
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


def _target(kind: str, value: str | None) -> dict | None:
    return None if value is None else {"kind": kind, "value": value}


def _intent(kind: str, *, subject: str | None = None) -> dict:
    return {
        "kind": kind,
        "subject": _target("entity", subject),
        "relation": None,
        "object": None,
        "relation_candidates": [],
        "operator": None,
        "value_type": None,
        "value": None,
        "reason": None,
    }


class IntentOnlyClient:
    name = "intent-only"

    def __init__(self, intent):
        self.intent = intent
        self.intent_calls = 0
        self.direct_datalog_calls = 0

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        self.intent_calls += 1
        raw = self.intent(question) if callable(self.intent) else self.intent
        return parse_query_intent(raw)

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        self.direct_datalog_calls += 1
        raise AssertionError("supported planner path must not call direct Datalog")


def test_init_scaffolds_policy(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    rc = cli.main(["init"])
    assert rc == 0
    policy = tmp_path / "policy" / "logic-policy.dl"
    assert policy.is_file()
    assert policy.read_text(encoding="utf-8") == DEFAULT_POLICY
    assert "policy:" in capsys.readouterr().out


def test_init_does_not_overwrite_existing_policy(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    policy = tmp_path / "policy" / "logic-policy.dl"
    policy.parent.mkdir(parents=True)
    policy.write_text("// my custom policy\n", encoding="utf-8")
    cli.main(["init"])
    assert policy.read_text(encoding="utf-8") == "// my custom policy\n"


def test_sync_file_inserts_candidates(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )
    src = tmp_path / "note.txt"
    src.write_text("hello", encoding="utf-8")

    rc = cli.main(["sync", str(src)])

    assert rc == 0
    assert "1 candidate(s)" in capsys.readouterr().out
    # the candidate is now queryable in the same KB
    s = Store(tmp_path / "kb.sqlite")
    assert [f["subject"] for f in s.review_queue()] == ["A"]


def test_sync_missing_file_errors(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    rc = cli.main(["sync", str(tmp_path / "nope.txt")])
    assert rc == 2
    assert "no such file" in capsys.readouterr().err


def test_ingest_registers_text_source(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "doc.txt"
    src.write_text("body", encoding="utf-8")

    rc = cli.main(["ingest", str(src)])

    assert rc == 0
    assert "text" in capsys.readouterr().out
    s = Store(tmp_path / "kb.sqlite")
    assert [(r["path"], r["kind"]) for r in s.sources_with_counts()] == [
        ("sources/doc.txt", "text")
    ]


def test_sync_uses_registered_artifact_after_binary_ingest(
    tmp_path, monkeypatch, capsys, fake_client
):
    _env(monkeypatch, tmp_path)
    register_converter(".rtfx", lambda raw: raw.decode("utf-8"))
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )
    src = tmp_path / "report.rtfx"
    src.write_bytes(b"artifact text")

    assert cli.main(["ingest", str(src)]) == 0
    assert cli.main(["sync"]) == 0

    out = capsys.readouterr().out
    assert "sources/report.rtfx: 1 candidate(s)" in out
    s = Store(tmp_path / "kb.sqlite")
    fact = s.review_queue()[0]
    job = s.get_extraction_job_detail(fact["job_id"])
    assert fact["source_path"] == "sources/report.rtfx"
    assert job["artifact_path"].startswith("artifacts/sources/")


def test_ingest_unsupported_type_errors(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "blob.bin"
    src.write_bytes(b"\x00\x01")
    rc = cli.main(["ingest", str(src)])
    assert rc == 1
    assert "unsupported source type" in capsys.readouterr().err


def test_sync_surfaces_llm_error(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(error=LLMError("provider down")),
    )
    src = tmp_path / "note.txt"
    src.write_text("hello", encoding="utf-8")

    rc = cli.main(["sync", str(src)])

    assert rc == 1
    assert "extraction failed: provider down" in capsys.readouterr().err


def test_sync_total_chunk_failure_exits_nonzero(
    tmp_path, monkeypatch, capsys, fake_client
):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "note.txt"
    src.write_text("body text", encoding="utf-8")
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(error=LLMError("provider down")),
    )

    rc = cli.main(["sync"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "chunk(s) failed" in captured.err
    assert "provider down" in captured.err
    assert "sync complete" not in captured.out


def test_sync_partial_chunk_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(src)]) == 0

    class _FlakyClient:
        # Fails the first chunk's extraction, succeeds after: one failed chunk
        # plus at least one completed chunk is the partial-failure shape.
        def __init__(self):
            self.calls = 0

        def extract_facts(self, *, source_text, schema_hint=""):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("first chunk hiccup")
            return [ExtractedFact("A", "is_a", "B", 0.9)]

    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _FlakyClient())

    rc = cli.main(["sync"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "1 candidate(s)" in captured.out
    assert "sync incomplete" in captured.err
    assert "sync failed" not in captured.err


def test_sync_mixed_success_and_total_source_failure_is_incomplete(
    tmp_path, monkeypatch, capsys
):
    # A run where one source fully succeeds with real candidates and another
    # totally fails is NOT a total failure: it must read "sync incomplete", so
    # the real candidates already on stdout are not disowned by a "failed" verdict.
    _env(monkeypatch, tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("good source body", encoding="utf-8")
    bad = tmp_path / "bad.txt"
    bad.write_text("fail marker body", encoding="utf-8")
    assert cli.main(["ingest", str(good)]) == 0
    assert cli.main(["ingest", str(bad)]) == 0

    class _KeyedClient:
        # Keyed on chunk content: every chunk of the "fail" source raises, the
        # other source yields one fact.
        def extract_facts(self, *, source_text, schema_hint=""):
            if "fail" in source_text:
                raise LLMError("provider down on this source")
            return [ExtractedFact("A", "is_a", "B", 0.9)]

    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _KeyedClient())

    rc = cli.main(["sync"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "good.txt: 1 candidate(s)" in captured.out
    assert "sync incomplete" in captured.err
    assert "sync failed" not in captured.err
    assert "reviewable" in captured.err
    assert "Analysis failed" in captured.err


def test_sync_all_complete_zero_candidates_is_success(
    tmp_path, monkeypatch, capsys, fake_client
):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "note.txt"
    src.write_text("body text", encoding="utf-8")
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: fake_client([]))

    rc = cli.main(["sync"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "sync complete" in captured.out
    assert "0 candidate(s)" in captured.out


def test_sync_empty_source_is_success(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    src = tmp_path / "blank.txt"
    src.write_text("   \n   \n", encoding="utf-8")
    assert cli.main(["ingest", str(src)]) == 0
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )

    rc = cli.main(["sync"])

    # Zero chunks means zero failed chunks: an empty source is a clean success,
    # never a total failure.
    assert rc == 0
    assert "sync failed" not in capsys.readouterr().err


def _register_two_chunk_source(tmp_path, monkeypatch) -> Path:
    """Ingest a registered source whose text splits into two chunks."""
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "note.txt"
    src.write_text(
        "alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa",
        encoding="utf-8",
    )
    assert cli.main(["ingest", str(src)]) == 0
    return src


def _stuck_running_job(tmp_path) -> int:
    """Leave the registered source's newest job `running` with a chunk in flight.

    Built the way `cmd_sync` would build it (same artifact, provider, model and
    chunk config), so once `--recover` rewinds it to `pending` the ordinary claim
    path resumes it instead of rebuilding a fresh job.
    """
    from verinote.pipeline import create_chunked_extraction_job

    cfg = config.Config.load()
    s = Store(tmp_path / "kb.sqlite")
    row = s.source_text_inputs()[0]
    text = (cfg.root / row["artifact_path"]).read_text(encoding="utf-8")
    job_id = create_chunked_extraction_job(
        s,
        source_id=int(row["source_id"]),
        artifact_id=int(row["artifact_id"]),
        source_text=text,
        provider=cfg.provider,
        model=cfg.model,
        chunk_chars=cfg.extraction_chunk_chars,
        chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
    )
    assert s.claim_pending_extraction_job(job_id) is True  # pending -> running
    s.mark_chunk_running(int(s.source_chunks(job_id)[0]["id"]))  # a crash mid-run
    s.close()
    return job_id


def _rolled_back_events(store, job_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM fact_events WHERE event_type = 'extraction_job_rolled_back'"
    params: tuple = ()
    if job_id is not None:
        sql += " AND job_id = ?"
        params = (job_id,)
    return store._conn.execute(sql, params).fetchone()["n"]


def test_sync_running_job_points_at_the_recovery_path(
    tmp_path, monkeypatch, capsys, fake_client
):
    # A plain sync must not touch a `running` job, and its skip line must name
    # BOTH possibilities (live worker vs. crashed mid-run) and the recovery path,
    # since the two are indistinguishable from DB state (#337).
    _env(monkeypatch, tmp_path)
    _register_two_chunk_source(tmp_path, monkeypatch)
    job_id = _stuck_running_job(tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )

    rc = cli.main(["sync"])

    err = capsys.readouterr().err
    assert rc == 0
    assert f"#{job_id} is already running, or was interrupted mid-run" in err
    assert "verinote sync --recover" in err
    s = Store(tmp_path / "kb.sqlite")
    assert s.get_extraction_job(job_id)["status"] == "running"
    assert _rolled_back_events(s) == 0


def test_sync_recover_resumes_a_stuck_running_job(
    tmp_path, monkeypatch, capsys, fake_client
):
    # The happy path: `--recover` rolls the stuck job back and the SAME invocation
    # resumes it to completion.
    _env(monkeypatch, tmp_path)
    _register_two_chunk_source(tmp_path, monkeypatch)
    job_id = _stuck_running_job(tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )

    rc = cli.main(["sync", "--recover"])

    err = capsys.readouterr().err
    assert rc == 0
    assert f"rolled back stuck extraction job #{job_id}" in err
    s = Store(tmp_path / "kb.sqlite")
    job = s.get_extraction_job(job_id)
    assert job["status"] == "done"
    assert int(job["completed_chunks"]) == int(job["total_chunks"]) == 2
    assert _rolled_back_events(s, job_id) == 1


def test_sync_recover_is_a_noop_on_a_done_job(tmp_path, monkeypatch, fake_client):
    # The most important guard: `rollback_extraction_job` does NOT self-guard a
    # `done` job, so a missing `status == 'running'` gate would rewind a finished
    # job and churn an empty run. `--recover` must leave it strictly alone.
    _env(monkeypatch, tmp_path)
    _register_two_chunk_source(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )
    assert cli.main(["sync"]) == 0
    s = Store(tmp_path / "kb.sqlite")
    done_id = int(s.source_extraction_jobs()[0]["id"])
    assert s.get_extraction_job(done_id)["status"] == "done"
    s.close()

    assert cli.main(["sync", "--recover"]) == 0

    s2 = Store(tmp_path / "kb.sqlite")
    assert _rolled_back_events(s2) == 0
    assert s2.get_extraction_job(done_id)["status"] == "done"


def test_sync_recover_is_a_noop_on_a_failed_job(tmp_path, monkeypatch, fake_client):
    # `rollback_extraction_job` does not self-guard a `failed` job either. Rewinding
    # one would resume it via the plain claim path, bypassing #323's retry/exhausted
    # machinery, so `--recover` must not touch it — it stays exactly where a plain,
    # non-`--recover` sync would leave it.
    _env(monkeypatch, tmp_path)
    _register_two_chunk_source(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(error=LLMError("provider down")),
    )
    assert cli.main(["sync"]) == 1  # every chunk fails -> job terminalises 'failed'
    s = Store(tmp_path / "kb.sqlite")
    failed_id = int(s.source_extraction_jobs()[0]["id"])
    assert s.get_extraction_job(failed_id)["status"] == "failed"
    s.close()

    assert cli.main(["sync", "--recover"]) == 1

    s2 = Store(tmp_path / "kb.sqlite")
    # never rolled back: a plain-claim resume of a failed job requires a prior
    # rollback to `pending`, which would have logged this event.
    assert _rolled_back_events(s2, failed_id) == 0
    assert s2.get_extraction_job(failed_id)["status"] == "failed"


def test_sync_recover_is_a_noop_on_a_canceled_job(tmp_path, monkeypatch, fake_client):
    # Consistent with `rollback_extraction_job`'s own `canceled` guard: a human
    # took it out of the queue, and `--recover` must not revive it.
    _env(monkeypatch, tmp_path)
    _register_two_chunk_source(tmp_path, monkeypatch)
    job_id = _stuck_running_job(tmp_path)
    s = Store(tmp_path / "kb.sqlite")
    s._conn.execute(
        "UPDATE extraction_jobs SET status = 'canceled' WHERE id = ?", (job_id,)
    )
    s.close()
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("A", "is_a", "B", 0.9)]),
    )

    assert cli.main(["sync", "--recover"]) == 0

    s2 = Store(tmp_path / "kb.sqlite")
    assert _rolled_back_events(s2, job_id) == 0
    assert s2.get_extraction_job(job_id)["status"] == "canceled"


def test_sync_recover_with_a_path_is_rejected(tmp_path, monkeypatch, capsys):
    # A path routes to the legacy non-chunked sync, which has no extraction job to
    # recover. Reject it rather than silently recovering nothing.
    _env(monkeypatch, tmp_path)
    src = tmp_path / "note.txt"
    src.write_text("hello", encoding="utf-8")

    rc = cli.main(["sync", str(src), "--recover"])

    assert rc == 2
    assert "--recover applies to registered sources" in capsys.readouterr().err


def test_query_adds_and_translates(tmp_path, monkeypatch, capsys, fake_client, intent_payload):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Subject", relation="is_a"
            )
        ),
    )
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    s.close()

    rc = cli.main(["query", "What is Sample Subject?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translated - Executable query is ready." in out
    assert "translated 1 question(s)" in out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "translated"
    assert (tmp_path / "facts" / "query.dl").is_file()


def test_query_relation_discovery_uses_planner_and_writes_only_translated_rules(
    tmp_path, monkeypatch, capsys
):
    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.add_fact(
        "Synthetic CLI Entity",
        "synthetic_cli_relation",
        "Synthetic CLI Value",
        status="confirmed",
    )
    store.close()
    client = IntentOnlyClient(
        _intent(
            "discover_entity_relations",
            subject="Synthetic CLI Entity",
        )
    )
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)

    rc = cli.main(["query", "How is Synthetic CLI Entity related?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translated - Executable query is ready." in out
    assert "translated 1 question(s)" in out
    assert client.direct_datalog_calls == 0
    store = Store(tmp_path / "kb.sqlite")
    assert store.questions()[0]["status"] == "translated"
    query_dl = (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8")
    assert (
        'answer_q1("synthetic_cli_relation") :- '
        'relation("Synthetic CLI Entity", "synthetic_cli_relation", O).'
    ) in query_dl
    assert "review_required" not in query_dl
    assert "ambiguous" not in query_dl
    assert "no_answer" not in query_dl


def test_query_relation_discovery_prints_public_lifecycle_outcomes(
    tmp_path, monkeypatch, capsys
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.add_fact(
        "Synthetic Review Entity", "source", "Synthetic Review Value", status="confirmed"
    )
    store.add_fact(
        "Synthetic Ambiguous Entity",
        "subject_relation",
        "Synthetic Subject Value",
        status="confirmed",
    )
    store.add_fact(
        "Synthetic Object Source",
        "object_relation",
        "Synthetic Ambiguous Entity",
        status="confirmed",
    )
    store.add_question("Review relation discovery?")
    store.add_question("Ambiguous relation discovery?")
    store.add_question("No answer relation discovery?")
    store.close()

    def intent_for(question: str):
        if question.startswith("Review"):
            return _intent(
                "discover_entity_relations", subject="Synthetic Review Entity"
            )
        if question.startswith("Ambiguous"):
            return _intent(
                "discover_entity_relations", subject="Synthetic Ambiguous Entity"
            )
        return _intent("discover_entity_relations", subject="Synthetic Missing Entity")

    client = IntentOnlyClient(intent_for)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: client)
    from verinote.pipeline.query import evaluate_query_candidate_plan as real_eval

    def no_answer_for_empty_plan(store, plan):
        if plan.reason == "no relation discovery candidates matched the schema":
            return QueryCandidateSetEvaluation(
                plan=plan,
                outcome=QueryCandidateSetOutcome.NO_ANSWER,
            )
        return real_eval(store, plan)

    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan",
        no_answer_for_empty_plan,
    )

    rc = cli.main(["query"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: review_required - relation label requires review: source" in out
    assert (
        "q2: ambiguous - multiple query candidates returned conflicting answers"
        in out
    )
    assert "q3: no_answer - no confirmed facts match" in out
    assert client.direct_datalog_calls == 0
    store = Store(tmp_path / "kb.sqlite")
    assert [q["status"] for q in store.questions()] == [
        "review_required",
        "ambiguous",
        "no_answer",
    ]
    query_dl = (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8")
    assert query_dl == ""


def test_query_persists_translation_failure_reason(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(error=LLMError("provider unavailable")),
    )

    rc = cli.main(["query", "What is the sample answer?"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "q1: translation_failed - provider unavailable" in captured.out
    assert "translated 0 question(s), 1 failed" in captured.out
    assert "provider unavailable" in captured.err
    s = Store(tmp_path / "kb.sqlite")
    q = s.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "provider unavailable"
    assert (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8") == ""


def test_query_persists_get_client_failure_reason(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)

    def raise_client_error(cfg):
        raise LLMError("missing provider credentials")

    monkeypatch.setattr("verinote.llm.get_client", raise_client_error)

    rc = cli.main(["query", "What is the sample answer?"])

    # Every question is marked translation_failed on a get_client failure, so the
    # run is a total failure: rc 1, with the reason surfaced and the count split.
    captured = capsys.readouterr()
    assert rc == 1
    assert "q1: translation_failed - missing provider credentials" in captured.out
    assert "translated 0 question(s), 1 failed" in captured.out
    assert "missing provider credentials" in captured.err
    s = Store(tmp_path / "kb.sqlite")
    q = s.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "missing provider credentials"
    assert (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8") == ""


def test_query_mixed_outcomes_exits_nonzero_with_split(
    tmp_path, monkeypatch, capsys, fake_client, intent_payload
):
    # One question translates, one fails: rc 1 (any failure), and the summary
    # splits the counts so the translated one is not hidden and the failure is
    # not hidden behind it.
    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    store.add_question("What is Sample Subject?")
    store.add_question("Please fail this question?")
    store.close()

    def intent_for(question):
        if "fail" in question:
            raise LLMError("provider rejected this question")
        return intent_payload("lookup_object", subject="Sample Subject", relation="is_a")

    monkeypatch.setattr(
        "verinote.llm.get_client", lambda cfg: fake_client(intent=intent_for)
    )

    rc = cli.main(["query"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "q1: translated" in captured.out
    assert "q2: translation_failed" in captured.out
    assert "translated 1 question(s), 1 failed" in captured.out
    assert "provider rejected this question" in captured.err


def test_query_no_pending_errors(tmp_path, monkeypatch, capsys):
    # A real KB with nothing pending keeps the existing diagnosis; only the
    # no-KB path routes through the #275 refusal.
    _env(monkeypatch, tmp_path)
    Store(tmp_path / "kb.sqlite").init_schema()

    rc = cli.main(["query"])

    assert rc == 1
    assert "no pending or failed questions" in capsys.readouterr().err


def test_query_retries_translation_failed_questions(
    tmp_path, monkeypatch, capsys, fake_client, intent_payload
):
    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.add_fact("Sample Subject", "is_a", "Synthetic Answer", status="confirmed")
    qid = store.add_question("What is Sample Subject?")
    store.set_question_query(qid, None, "translation_failed", "provider returned invalid schema")
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Subject", relation="is_a"
            )
        ),
    )

    rc = cli.main(["query"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translated - Executable query is ready." in out
    assert "translated 1 question(s)" in out
    q = Store(tmp_path / "kb.sqlite").questions()[0]
    assert q["status"] == "translated"
    assert q["reason"] == ""


def test_repair_validates_and_translates(
    tmp_path, monkeypatch, capsys, fake_client, intent_payload
):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Person", relation="born_in"
            )
        ),
    )
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    qid = s.add_question("Where was Sample Person born?")
    s.set_question_query(
        qid, 'review_required("Where was Sample Person born?")', "review_required"
    )
    s.close()

    rc = cli.main(["repair"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translated - Executable query is ready." in out
    assert "repaired 1/1" in out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "translated"


def test_repair_reports_durable_rejected_status(
    tmp_path, monkeypatch, capsys, fake_client, intent_payload
):
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetEvaluation
    from verinote.pipeline.query_candidate_eval import QueryCandidateSetOutcome

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            intent=intent_payload(
                "lookup_object", subject="Sample Person", relation="born_in"
            )
        ),
    )
    monkeypatch.setattr(
        "verinote.pipeline.query.evaluate_query_candidate_plan",
        lambda store, plan: QueryCandidateSetEvaluation(
            plan=plan, outcome=QueryCandidateSetOutcome.NO_ANSWER
        ),
    )
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    qid = s.add_question("Where was Sample Person born?")
    s.set_question_query(
        qid, 'review_required("Where was Sample Person born?")', "review_required"
    )
    s.close()

    rc = cli.main(["repair"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: no_answer - no confirmed facts match" in out
    assert "repaired 0/1" in out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "no_answer"


def test_query_prints_review_required_outcome(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: object())

    def translate(store, client, *, root, allow_direct_datalog_fallback=False):
        q = store.questions(pending_only=True)[0]
        reason = "unsupported synthetic question"
        store.set_question_query(
            q["id"], f'review_required("{reason}")', "review_required", reason
        )
        return [{"id": q["id"], "status": "review_required", "reason": reason}]

    monkeypatch.setattr("verinote.pipeline.translate_questions", translate)

    rc = cli.main(["query", "What synthetic relation is missing?"])

    assert rc == 0
    assert (
        "q1: review_required - unsupported synthetic question"
        in capsys.readouterr().out
    )


def test_query_prints_no_answer_outcome(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: object())

    def translate(store, client, *, root, allow_direct_datalog_fallback=False):
        q = store.questions(pending_only=True)[0]
        reason = "no confirmed facts match"
        store.set_question_query(q["id"], f'no_answer("{reason}")', "no_answer", reason)
        return [{"id": q["id"], "status": "no_answer", "reason": reason}]

    monkeypatch.setattr("verinote.pipeline.translate_questions", translate)

    rc = cli.main(["query", "Which synthetic answer exists?"])

    assert rc == 0
    assert "q1: no_answer - no confirmed facts match" in capsys.readouterr().out


def test_query_prints_ambiguous_outcome(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: object())

    def translate(store, client, *, root, allow_direct_datalog_fallback=False):
        q = store.questions(pending_only=True)[0]
        reason = "multiple synthetic candidates matched"
        store.set_question_query(q["id"], f'ambiguous("{reason}")', "ambiguous", reason)
        return [{"id": q["id"], "status": "ambiguous", "reason": reason}]

    monkeypatch.setattr("verinote.pipeline.translate_questions", translate)

    rc = cli.main(["query", "Which synthetic candidate matches?"])

    assert rc == 0
    assert "q1: ambiguous - multiple synthetic candidates matched" in capsys.readouterr().out


def test_repair_no_review_required_errors(tmp_path, monkeypatch, capsys):
    # As above: the existing message belongs to the KB-exists path.
    _env(monkeypatch, tmp_path)
    Store(tmp_path / "kb.sqlite").init_schema()

    rc = cli.main(["repair"])

    assert rc == 1
    assert "no review_required questions" in capsys.readouterr().err


def test_coverage_strict_gates_on_gap(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="needs_review", source_id=sid)  # not engine input
    s.close()

    assert cli.main(["coverage"]) == 0  # non-strict always succeeds
    assert cli.main(["coverage", "--strict"]) == 1  # a gap exists
    assert "GAP" in capsys.readouterr().out


def test_coverage_counts_structural_engine_fact_metadata(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    sid = s.add_source("sources/a.txt")
    s.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="confirmed",
        source_id=sid,
    )
    s.close()

    assert cli.main(["coverage", "--strict"]) == 0
    out = capsys.readouterr().out
    assert "sources/a.txt: 1/1 engine facts" in out
    assert "gap(s)" in out


# --- local KB commands must not target the saved (global) active KB ----------


def _isolated(monkeypatch, tmp_path):
    """Neutralise every ambient KB pointer, then give the app config a fake HOME."""
    monkeypatch.delenv("VERINOTE_ROOT", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    return home


def _existing_kb(tmp_path) -> Path:
    """A populated KB registered as the saved active root (as the web picker does)."""
    root = tmp_path / "existing"
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "kb.sqlite")
    store.init_schema()
    sid = store.add_source("sources/real.txt")
    store.add_fact("Real Org", "is_a", "participant", status="confirmed", source_id=sid)
    store.close()
    config.save_active_root(root)
    return root


def test_init_seed_in_empty_dir_ignores_saved_active_kb(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    existing = _existing_kb(tmp_path)
    before = (existing / "kb.sqlite").read_bytes()

    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["init", "--seed"]) == 0

    new_db = workdir / "data" / "kb.sqlite"
    assert new_db.is_file()
    # The saved KB is untouched, byte for byte.
    assert (existing / "kb.sqlite").read_bytes() == before
    assert not (existing / "sources").exists()
    # And `init` must not repoint the global active KB at what it just made:
    # a local command may never mutate global state (that is the whole issue).
    assert config.read_app_config()["active_root"] == str(existing)

    out = capsys.readouterr().out
    assert f"initialised KB at {workdir / 'data'}" in out
    assert str(existing) not in out


def test_init_uses_explicit_root_argument(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    target = tmp_path / "named-kb"

    assert cli.main(["init", str(target)]) == 0

    assert (target / "kb.sqlite").is_file()
    assert not (workdir / "data").exists()
    assert f"initialised KB at {target}" in capsys.readouterr().out


def test_init_still_honours_verinote_root(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    _existing_kb(tmp_path)
    env_root = tmp_path / "from-env"
    monkeypatch.setenv("VERINOTE_ROOT", str(env_root))
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["init"]) == 0

    assert (env_root / "kb.sqlite").is_file()
    assert not (workdir / "data").exists()


def test_demo_facts_are_never_engine_input():
    demo_statuses = {status for _, _, _, status, _, _, _ in cli._DEMO_FACTS}
    # Asked through the accessor, not a frozenset imported at module load: an
    # import binds the tier at import time, which is the split #176 closed.
    engine = engine_statuses()
    # Both sides must be non-empty, else the disjointness check below is vacuous.
    assert demo_statuses
    assert engine
    assert demo_statuses & engine == set()


def test_seeded_kb_has_no_engine_facts(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "kb"

    assert cli.main(["init", str(root), "--seed"]) == 0

    store = Store(root / "kb.sqlite")
    assert store.facts(statuses=engine_statuses()) == []
    assert store.facts() != []  # the demo facts did land, just not as engine input
    store.close()


def test_seed_without_a_kb_fails_and_creates_nothing(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["seed"]) == 1

    assert not (workdir / "data").exists()
    assert "run `verinote init` first" in capsys.readouterr().err


def test_seed_targets_the_named_root(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    existing = _existing_kb(tmp_path)
    before = (existing / "kb.sqlite").read_bytes()
    root = tmp_path / "kb"
    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(["seed", str(root)]) == 0

    store = Store(root / "kb.sqlite")
    assert len(store.facts()) == len(cli._DEMO_FACTS)
    store.close()
    assert (existing / "kb.sqlite").read_bytes() == before
    assert f"seeded demo facts into {root}" in capsys.readouterr().out


def test_seed_run_twice_does_not_double_demo_facts(tmp_path):
    # Seed routes through reconcile_fact, so a second seed over the same demo
    # (source, triple) pairs re-hits the existing rows instead of duplicating them.
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()

    cli._seed(store)
    cli._seed(store)

    assert len(store.facts()) == len(cli._DEMO_FACTS)
    store.close()


def test_init_help_does_not_promise_verinote_root_only(capsys):
    with pytest.raises(SystemExit):
        cli.main(["init", "--help"])
    out = capsys.readouterr().out
    assert "under VERINOTE_ROOT (./data)" not in out
    assert "ROOT" in out


def test_init_tells_you_how_to_actually_use_the_kb_it_made(tmp_path, monkeypatch, capsys):
    """The KB `init` creates is not the active one, so the hint must name it."""
    _isolated(monkeypatch, tmp_path)
    _existing_kb(tmp_path)  # a different KB is the saved active one
    root = tmp_path / "fresh"

    assert cli.main(["init", str(root), "--seed"]) == 0

    out = capsys.readouterr().out
    assert f"VERINOTE_ROOT={root} verinote status" in out
    # It must not claim a bare `verinote status` would show this KB.
    assert "(run `verinote status`)" not in out


def test_init_rejects_a_root_that_is_an_existing_file(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    target = tmp_path / "afile.txt"
    target.write_text("not a KB\n", encoding="utf-8")

    assert cli.main(["init", str(target)]) == 1

    err = capsys.readouterr().err
    assert "not a directory" in err
    assert target.read_text(encoding="utf-8") == "not a KB\n"


def test_init_rejects_an_empty_root_argument(tmp_path, monkeypatch, capsys):
    """An empty ROOT must fail, not silently fall back to ./data."""
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["init", ""]) == 1

    assert not (workdir / "data").exists()
    assert "empty string" in capsys.readouterr().err


def test_seed_rejects_an_empty_root_argument(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["seed", ""]) == 1

    assert not (workdir / "data").exists()
    assert "empty string" in capsys.readouterr().err


def test_seed_rejects_an_empty_db_file_instead_of_creating_a_schema(
    tmp_path, monkeypatch, capsys
):
    """`kb.sqlite` existing is not enough: seed only fills an *initialised* KB."""
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "broken"
    root.mkdir()
    db = root / "kb.sqlite"
    db.write_bytes(b"")

    assert cli.main(["seed", str(root)]) == 1

    assert db.read_bytes() == b""  # no schema was created behind our back
    err = capsys.readouterr().err
    assert "is not a verinote KB" in err
    assert f"verinote init {root}" in err  # recovery path


def test_seed_rejects_a_corrupt_db_file_without_a_traceback(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "corrupt"
    root.mkdir()
    db = root / "kb.sqlite"
    db.write_bytes(b"definitely not sqlite\n")

    assert cli.main(["seed", str(root)]) == 1

    assert db.read_bytes() == b"definitely not sqlite\n"
    err = capsys.readouterr().err
    assert "is not a verinote KB" in err
    assert f"verinote init {root}" in err


def test_init_refuses_a_corrupt_db_instead_of_raising(tmp_path, monkeypatch, capsys):
    # The file may be a real KB with a damaged header — every review decision the
    # user ever made — so `init` must not scaffold over it, and must not hand them
    # a raw sqlite3 traceback either.
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "corrupt"
    root.mkdir()
    db = root / "kb.sqlite"
    db.write_bytes(b"definitely not sqlite\n")

    assert cli.main(["init", str(root)]) == 1

    assert db.read_bytes() == b"definitely not sqlite\n"
    assert not (root / "policy").exists(), "init scaffolded onto an unreadable KB"
    err = capsys.readouterr().err
    assert cli.KB_UNREADABLE in err
    assert "restore it from backup" in err


def test_init_still_scaffolds_a_database_that_merely_has_no_schema(tmp_path, monkeypatch):
    # The other half of the same check: an empty `kb.sqlite` *is* a readable
    # database with no schema, and putting a schema into it is exactly `init`'s
    # job. Collapsing the two states into "unusable" would break the fresh-KB path.
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "empty"
    root.mkdir()
    (root / "kb.sqlite").touch()

    assert cli.main(["init", str(root)]) == 0

    store = Store(root / "kb.sqlite")
    assert store.facts() == []
    store.close()
    assert (root / "policy" / "logic-policy.dl").is_file()


def _tables(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def test_init_refuses_a_foreign_facts_table(tmp_path, monkeypatch, capsys):
    # A `facts` table that is not verinote's must stop init BEFORE any write: the
    # load-bearing assertion is that the foreign file is left byte-for-byte as it
    # was, because `executescript` autocommits per statement and would otherwise
    # scatter verinote's tables into it before crashing on the mismatch (#290).
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "foreign"
    root.mkdir()
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE facts(a, b)")
    conn.commit()
    conn.close()

    assert cli.main(["init", str(root)]) == 1

    err = capsys.readouterr().err
    assert "is not a verinote KB" in err
    assert cli.KB_ALIEN_FACTS in err
    assert "Traceback" not in err
    assert _tables(db) == {"facts"}  # nothing verinote added
    assert not (root / "policy").exists()


def test_init_refuses_a_partial_schema(tmp_path, monkeypatch, capsys):
    # A verinote-shaped `facts` alone is a partial schema, not an empty slot:
    # `IF NOT EXISTS` would complete it while writing over the user's file.
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "partial"
    root.mkdir()
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, subject, relation, object, status)"
    )
    conn.commit()
    conn.close()

    assert cli.main(["init", str(root)]) == 1

    err = capsys.readouterr().err
    assert "is not a verinote KB" in err
    assert cli.KB_PARTIAL_SCHEMA in err
    assert _tables(db) == {"facts"}


def test_init_seed_on_a_foreign_facts_table_writes_nothing(tmp_path, monkeypatch, capsys):
    # `--seed` must not reach `_seed`: the refusal returns before the store is
    # ever opened, so no demo facts land in the foreign database.
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "foreign"
    root.mkdir()
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE facts(a, b)")
    conn.commit()
    conn.close()

    assert cli.main(["init", str(root), "--seed"]) == 1

    assert "is not a verinote KB" in capsys.readouterr().err
    assert _tables(db) == {"facts"}
    assert list(sqlite3.connect(db).execute("SELECT * FROM facts")) == []


def test_init_twice_is_idempotent(tmp_path, monkeypatch, capsys):
    # A healthy, already-initialised KB re-init'd is a no-op re-init, not a refusal.
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "kb"

    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(["init", str(root)]) == 0
    assert "is not a verinote KB" not in capsys.readouterr().err


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows forbids `?` in paths")
def test_kb_schema_probe_survives_a_uri_metacharacter_in_the_path(tmp_path):
    """A `?` in the root must not truncate the SQLite URI we probe the schema with.

    Interpolating the path raw into `file:{db}?mode=ro` cut the URI at the `?`,
    which (a) misreported this healthy KB as having no `facts` table and (b) lost
    `mode=ro`, so SQLite opened read-write and created a stray file at the
    truncated path — writing outside the root the caller ever named.
    """
    parent = tmp_path / "parent"
    root = parent / "weird?dir"
    root.mkdir(parents=True)
    store = Store(root / "kb.sqlite")
    store.init_schema()
    store.close()
    before = sorted(p.name for p in parent.iterdir())

    assert cli._kb_schema_problem(root / "kb.sqlite") is None  # (a) no false alarm

    # (b) no stray file at the truncated path (`.../parent/weird`).
    assert sorted(p.name for p in parent.iterdir()) == before == ["weird?dir"]


def test_seed_accepts_a_kb_whose_path_holds_a_uri_metacharacter(tmp_path, monkeypatch, capsys):
    """End-to-end twin of the probe test, through the command a user actually runs.

    Uses `#` (a URI fragment marker, and legal on every platform) rather than `?`
    so the assertion stays on the schema probe.
    """
    _isolated(monkeypatch, tmp_path)
    parent = tmp_path / "parent"
    root = parent / "weird#dir"
    root.mkdir(parents=True)

    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()
    before = sorted(p.name for p in parent.iterdir())

    assert cli.main(["seed", str(root)]) == 0  # no false "is not a verinote KB"

    assert "seeded demo facts" in capsys.readouterr().out
    assert sorted(p.name for p in parent.iterdir()) == before == ["weird#dir"]

    store = Store(root / "kb.sqlite")
    assert len(store.facts()) == len(cli._DEMO_FACTS)
    store.close()


def test_status_on_absent_kb_refuses_and_creates_nothing(tmp_path, monkeypatch, capsys):
    """A read-only diagnosis must not scaffold a fresh KB at a mistyped path."""
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["status"]) == 1

    assert not (workdir / "data").exists()  # no KB was created behind our back
    err = capsys.readouterr().err
    assert "no KB" in err
    assert str(workdir / "data") in err


def test_coverage_on_absent_kb_refuses_and_creates_nothing(tmp_path, monkeypatch, capsys):
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["coverage"]) == 1

    assert not (workdir / "data").exists()
    err = capsys.readouterr().err
    assert "no KB" in err
    assert str(workdir / "data") in err


def test_status_rejects_an_empty_db_file_instead_of_creating_a_schema(
    tmp_path, monkeypatch, capsys
):
    """`kb.sqlite` existing is not enough: status reads an *initialised* KB."""
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "broken"
    root.mkdir()
    db = root / "kb.sqlite"
    db.write_bytes(b"")
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["status"]) == 1

    assert db.read_bytes() == b""  # no schema was created behind our back
    assert "is not a verinote KB" in capsys.readouterr().err


def test_coverage_rejects_a_corrupt_db_file_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "corrupt"
    root.mkdir()
    db = root / "kb.sqlite"
    db.write_bytes(b"not sqlite")
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["coverage"]) == 1

    assert db.read_bytes() == b"not sqlite"  # file left untouched, no traceback
    assert "is not a verinote KB" in capsys.readouterr().err


def _alien_facts_db(root: Path) -> Path:
    """A readable SQLite file holding a `facts` table that is not verinote's.

    The shape a half-finished script or an unrelated project leaves behind: the
    table name matches, so a name-only probe calls it a KB, but none of the
    columns the KB is made of are there.
    """
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_commands_refuse_a_foreign_facts_table_without_touching_it(
    command, tmp_path, monkeypatch, capsys
):
    """A `facts` table alone is not a KB, and the file is not ours to migrate.

    Probing for the *name* `facts` let this file through as healthy. `_store()`
    then ran `init_schema()` on it, which failed on the mismatched table with a
    raw `sqlite3.OperationalError` — but only after `CREATE TABLE IF NOT EXISTS`
    had already added six tables to a file the user asked us to *read*. A
    diagnosis that half-migrates whatever it is pointed at is worse than one that
    refuses: the write is the damage, and the traceback merely reports it.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "alien"
    db = _alien_facts_db(root)
    before = db.read_bytes()
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 1

    assert db.read_bytes() == before  # not one byte of a file we were asked to read
    assert sorted(p.name for p in root.iterdir()) == ["kb.sqlite"]  # no -wal, no policy
    err = capsys.readouterr().err
    assert "is not a verinote KB" in err
    assert "Traceback" not in err


def test_read_only_commands_still_accept_a_kb_predating_the_job_id_column(
    tmp_path, monkeypatch, capsys
):
    """The other edge of the same check: it must not reject KBs migration exists to fix.

    `facts.job_id` is added by `_ensure_schema_migrations()`, so a KB written
    before that column existed is *valid* and self-heals on open. Demanding the
    current schema's full column set here would refuse those KBs outright — which
    is why the guard asks only for the columns every verinote KB has ever had.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "legacy"
    root.mkdir()
    store = Store(root / "kb.sqlite")
    store.init_schema()
    store.close()
    conn = sqlite3.connect(root / "kb.sqlite")
    conn.execute("ALTER TABLE facts DROP COLUMN job_id")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["status"]) == 0

    assert "KB:" in capsys.readouterr().out


def _alien_sources_db(root: Path) -> Path:
    """A verinote `facts` table beside somebody else's `sources` table.

    Passes the probe, then fails deeper in on `SELECT ... ORDER BY path`.

    The core tables are all present and only `sources`' *columns* are alien, which
    is what keeps this file past the probe: the probe asks which tables are there,
    not what every column of each one looks like — checking that would be the
    schema-drift trap it is shaped to avoid. So this file is exactly the case the
    probe cannot catch by construction, which is why the wrap has to.
    """
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, relation TEXT,
            object TEXT, status TEXT
        );
        CREATE TABLE sources (nonsense TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY);
        CREATE TABLE review_log (id INTEGER PRIMARY KEY);
        """
    )
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_commands_diagnose_an_alien_table_past_the_facts_probe(
    command, tmp_path, monkeypatch, capsys
):
    """The probe now rejects alien core-table columns before opening read-write.

    PR #274 originally let this file reach `_store().init_schema()`, then wrapped
    the later SQL error. Issue #291 moves the boundary earlier: `sources` is one
    of the oldest KB tables, and its `id`/`path` columns are part of the
    historical read contract, so a table without them is refused without touching
    the file.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "alien-sources"
    _alien_sources_db(root)
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 1

    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "is not a verinote KB" in out.err


def _table_names(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _root_file_names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_diagnostics_leave_a_current_kb_untouched(
    command, tmp_path, monkeypatch, capsys
):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "healthy"
    root.mkdir()
    store = Store(root / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("sources/sample.txt")
    store.add_fact("A", "r", "B", status="confirmed", source_id=source_id)
    store.close()
    db = root / "kb.sqlite"
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    before_files = _root_file_names(root)
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 0

    assert "policy:" in capsys.readouterr().out
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    assert _root_file_names(root) == before_files


def test_status_reads_a_live_wal_kb_without_false_schema_refusal(
    tmp_path, monkeypatch, capsys
):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "live-wal"
    root.mkdir()
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, relation TEXT,
            object TEXT, status TEXT
        );
        CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
        CREATE TABLE runs (id INTEGER PRIMARY KEY);
        CREATE TABLE review_log (id INTEGER PRIMARY KEY);
        INSERT INTO sources(id, path) VALUES (1, 'sources/sample.txt');
        INSERT INTO facts(id, subject, relation, object, status)
            VALUES (1, 'A', 'r', 'B', 'confirmed');
        """
    )
    conn.commit()
    assert (root / "kb.sqlite-wal").exists()
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    try:
        assert cli.main(["status"]) == 0
    finally:
        conn.close()

    out = capsys.readouterr()
    assert "facts:   1" in out.out
    assert "is not a verinote KB" not in out.err


def test_status_does_not_adopt_a_pre_marker_policy_file_read_only(
    tmp_path, monkeypatch, capsys
):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "pre-marker-policy"
    root.mkdir()
    store = Store(root / "kb.sqlite")
    store.init_schema()
    store.close()
    policy = root / "policy" / "logic-policy.dl"
    policy.parent.mkdir()
    policy.write_text("// synthetic policy\n", encoding="utf-8")
    db = root / "kb.sqlite"
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["status"]) == 0

    assert "policy: ok" in capsys.readouterr().out
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    conn = sqlite3.connect(db)
    marker = conn.execute(
        "SELECT value FROM kb_meta WHERE key = 'policy.logic'"
    ).fetchone()
    conn.close()
    assert marker is None


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_commands_do_not_modify_an_alien_sources_table(
    command, tmp_path, monkeypatch, capsys
):
    """Even deeper malformed tables must not be half-migrated by diagnostics."""
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "alien-sources-readonly"
    db = _alien_sources_db(root)
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    before_tables = _table_names(db)
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 1

    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "is not a verinote KB" in out.err
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    assert _table_names(db) == before_tables
    assert sorted(p.name for p in root.iterdir()) == ["kb.sqlite"]


def _sources_with_null_path_db(root: Path) -> Path:
    """A malformed DB that passes shape checks but has invalid source data."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT, relation TEXT,
            object TEXT, status TEXT, source_id INTEGER
        );
        CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT, kind TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY);
        CREATE TABLE review_log (id INTEGER PRIMARY KEY);
        INSERT INTO sources(id, path, kind) VALUES (1, NULL, 'text');
        """
    )
    conn.commit()
    conn.close()
    return db


def test_coverage_refuses_null_source_path_without_traceback_or_write(
    tmp_path, monkeypatch, capsys
):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "null-source-path"
    db = _sources_with_null_path_db(root)
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    before_tables = _table_names(db)
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["coverage"]) == 1

    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "cannot read the KB" in out.err
    assert "unsupported operand" not in out.err
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    assert _table_names(db) == before_tables


def test_the_two_refusal_paths_do_not_borrow_each_others_diagnosis(
    tmp_path, monkeypatch, capsys
):
    """The probe refusal and the wrap diagnosis describe different worlds.

    The probe refuses *before* anything opens the KB read-write, so that file is
    provably untouched. The wrap only ever fires after `_store()` has already
    written. Leaking the wrap's "restore from backup" into the probe's path would
    tell a user to recover a file nothing has touched; leaking the probe's flat
    "not a verinote KB" into the wrap's path would state a cause we cannot know.
    """
    _isolated(monkeypatch, tmp_path)
    probe_root = tmp_path / "probe"
    probe_db = _alien_facts_db(probe_root)
    before = probe_db.read_bytes()
    monkeypatch.setenv("VERINOTE_ROOT", str(probe_root))

    assert cli.main(["status"]) == 1

    probe_err = capsys.readouterr().err
    assert probe_db.read_bytes() == before  # nothing to restore, and we say nothing
    assert "is not a verinote KB" in probe_err
    assert "backup" not in probe_err
    assert "bug in verinote" not in probe_err

    wrap_root = tmp_path / "wrap"
    _sources_with_null_path_db(wrap_root)
    monkeypatch.setenv("VERINOTE_ROOT", str(wrap_root))

    assert cli.main(["coverage"]) == 1

    wrap_err = capsys.readouterr().err
    assert "cannot read the KB" in wrap_err
    assert "is not a verinote KB" not in wrap_err


def _partial_schema_db(root: Path) -> Path:
    """A verinote-shaped `facts` table and nothing else — not a KB.

    Every column the identity probe asks for is here, so a facts-only probe
    calls this a KB. But no verinote KB has ever consisted of `facts` alone:
    `sources`, `runs` and `review_log` have been in `schema.sql` since the
    initial commit, so a file missing them was never written by verinote.
    """
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT, "
        "relation TEXT, object TEXT, status TEXT)"
    )
    conn.commit()
    conn.close()
    return db


def _core_tables_with_idless_facts_db(root: Path) -> Path:
    """Core table names are not enough when `facts` lacks its row identity."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kb.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE facts (
            subject TEXT, relation TEXT, object TEXT, status TEXT
        );
        CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
        CREATE TABLE runs (id INTEGER PRIMARY KEY);
        CREATE TABLE review_log (id INTEGER PRIMARY KEY);
        """
    )
    conn.commit()
    conn.close()
    return db


def test_read_only_commands_still_accept_a_kb_predating_the_later_tables(
    tmp_path, monkeypatch, capsys
):
    """The other edge of the core-table check, and the one that bounds its reach.

    `schema.sql` grew from four tables to eleven, and `init_schema()` runs it on
    every open — so `CREATE TABLE IF NOT EXISTS` is how `kb_meta`, `fact_events`
    and the rest reach KBs written before they existed. Demanding the *current*
    table set here would refuse those KBs outright, which is why the check asks
    only for the four tables the schema has had since its initial commit.

    So this is a KB from before any of the later tables existed: it must still
    open and self-heal, exactly as the missing-column case above does. The
    sibling test proves a `facts` table alone is refused; this one proves the
    refusal stops there and does not reach real KBs.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "ancient"
    root.mkdir()
    conn = sqlite3.connect(root / "kb.sqlite")
    conn.executescript(
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, started_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, subject TEXT NOT NULL, relation TEXT NOT NULL,
            object TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate'
        );
        CREATE TABLE review_log (
            id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL,
            action TEXT NOT NULL, at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main(["status"]) == 0

    assert "KB:" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_commands_refuse_a_partial_schema_instead_of_completing_it(
    command, tmp_path, monkeypatch, capsys
):
    """A file with only `facts` must not be finished into a KB and reported healthy.

    The identity probe reads the `facts` columns, which this file has, so it used
    to get through — and then `_store()` ran `init_schema()`, whose
    `CREATE TABLE IF NOT EXISTS` *completed* the rest of the schema. The command
    then found a coherent (empty) KB and reported rc=0. That is the worst of both
    outcomes this PR exists to prevent: a read-only diagnosis wrote to the file,
    and then vouched for what it had just written.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "partial"
    db = _partial_schema_db(root)
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 1

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "is not a verinote KB" in err
    # The probe refuses before anything opens the file read-write, so the bytes
    # *and* the mtime are the evidence that nothing ran `init_schema()` here.
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    # The tables `init_schema()` would have added are the tell: if any of these
    # exist, the read-only command wrote the schema it then reported on.
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"facts"}


@pytest.mark.parametrize("command", ["status", "coverage"])
def test_read_only_commands_refuse_core_tables_when_facts_lacks_id(
    command, tmp_path, monkeypatch, capsys
):
    """`facts.id` is part of the oldest KB identity, not a migratable column.

    Without this guard, a file with verinote-looking core table names but no
    `facts.id` reaches `_store().init_schema()`, gains later migration tables,
    and then fails deeper in a command that was supposed to read only.
    """
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "idless-facts"
    db = _core_tables_with_idless_facts_db(root)
    before = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    monkeypatch.setenv("VERINOTE_ROOT", str(root))

    assert cli.main([command]) == 1

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "is not a verinote KB" in err
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == before_mtime
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"facts", "review_log", "runs", "sources"}


# --- #275: decide there is nothing to do before scaffolding a KB -----------


def test_query_with_no_argument_creates_no_kb(tmp_path, monkeypatch, capsys):
    """No question and no KB means no work — and so no scaffolding (#275)."""
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["query"]) == 1

    assert not (workdir / "data").exists()
    assert not (workdir / "data" / "kb.sqlite").exists()
    assert "no KB" in capsys.readouterr().err


def test_repair_creates_no_kb_when_there_is_none(tmp_path, monkeypatch, capsys):
    # `repair` takes no arguments, so a missing KB is always a no-op.
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["repair"]) == 1

    assert not (workdir / "data").exists()
    assert "no KB" in capsys.readouterr().err


def test_sync_with_nothing_to_sync_creates_no_kb(tmp_path, monkeypatch, capsys):
    # No path, no KB, no source files. Sync keeps its OWN diagnosis here — the
    # user's problem is that there is nothing to sync, not that a KB is missing.
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["sync"]) == 1

    assert not (workdir / "data").exists()
    assert not (workdir / "data" / "kb.sqlite").exists()
    err = capsys.readouterr().err
    assert "no sources to sync" in err
    assert "no KB" not in err


def test_sync_scaffolds_when_source_files_exist_without_a_kb(tmp_path, monkeypatch):
    # The workflow the guard must not break: drop files under sources/ and sync.
    # There IS work to do, so scaffolding the KB is legitimate.
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "work"
    sources = workdir / "data" / "sources"
    sources.mkdir(parents=True)
    (sources / "x.txt").write_text("body", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: (_ for _ in ()).throw(LLMError("provider down")),
    )

    cli.main(["sync"])

    # It got past the guard and opened the KB — that is what is being pinned.
    assert (workdir / "data" / "kb.sqlite").is_file()


def test_sync_with_a_path_argument_still_scaffolds(tmp_path, monkeypatch):
    # An explicit path is work by itself, so the guard must not fire on it.
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    note = workdir / "note.txt"
    note.write_text("body", encoding="utf-8")
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: (_ for _ in ()).throw(LLMError("provider down")),
    )

    cli.main(["sync", str(note)])

    assert (workdir / "data" / "kb.sqlite").is_file()


def test_sync_on_an_existing_kb_with_no_sources_keeps_its_message(
    tmp_path, monkeypatch, capsys
):
    # With a KB present the guard cannot fire, so the existing
    # open-resolve-refuse path runs and its message is unchanged.
    _env(monkeypatch, tmp_path)
    Store(tmp_path / "kb.sqlite").init_schema()
    (tmp_path / "sources").mkdir()

    assert cli.main(["sync"]) == 1

    assert "no sources to sync" in capsys.readouterr().err


def test_query_with_a_question_still_creates_a_kb(tmp_path, monkeypatch):
    # A question is real work, so scaffolding is the point. rc is 1 here per the
    # #243 contract (the translation fails); the KB creation is what matters.
    _isolated(monkeypatch, tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: (_ for _ in ()).throw(LLMError("provider down")),
    )

    cli.main(["query", "What is X?"])

    db_path = workdir / "data" / "kb.sqlite"
    assert db_path.is_file()
    store = Store(db_path)
    assert [q["text"] for q in store.questions()] == ["What is X?"]
    store.close()


def test_query_with_a_saved_active_kb_elsewhere_creates_nothing_in_cwd(
    tmp_path, monkeypatch, capsys
):
    """The guard judges the RESOLVED cfg.db_path, not the directory you stand in.

    #185 adjacency: with no VERINOTE_ROOT and a saved active KB pointing
    elsewhere, `verinote query` must work against that KB and leave the cwd
    untouched — never scaffolding a stray `./data` beside the user.
    """
    _isolated(monkeypatch, tmp_path)
    existing = _existing_kb(tmp_path)
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert cli.main(["query"]) == 1

    assert not (workdir / "data").exists()
    # It reached the real (saved) KB rather than refusing it.
    assert "no pending or failed questions" in capsys.readouterr().err
    assert (existing / "kb.sqlite").is_file()


def test_sync_returns_a_stale_confirmed_fact_to_review(
    tmp_path, monkeypatch, capsys, fake_client
):
    # #329 end to end through the CLI: a confirmed citation whose source text has
    # since changed is returned to the review queue, and cmd_sync's per-source line
    # reports it.
    _env(monkeypatch, tmp_path)
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    sid = s.add_source("sources/a.txt", kind="text")
    old = s.add_source_artifact(
        source_id=sid, kind="original_text", path="sources/a-v1.txt", checksum="v1"
    )
    london = s.add_fact("Ada", "born_in", "London", status="confirmed", source_id=sid)
    s.add_fact_evidence(fact_id=london, source_id=sid, artifact_id=old, snippet="London")
    # The edited text is the source's current artifact; its file is on disk so the
    # no-path sync resolves and re-extracts it.
    art_dir = tmp_path / "artifacts" / "sources" / str(sid)
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "v2.txt").write_text("Ada was born in Paris.", encoding="utf-8")
    s.add_source_artifact(
        source_id=sid,
        kind="original_text",
        path=f"artifacts/sources/{sid}/v2.txt",
        checksum="v2",
    )
    s.close()
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client([ExtractedFact("Ada", "born_in", "Paris", 0.9)]),
    )

    rc = cli.main(["sync"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "1 confirmed fact(s) returned to review" in out
    s2 = Store(tmp_path / "kb.sqlite")
    london_row = next(f for f in s2.facts() if f["object"] == "London")
    assert london_row["status"] == "needs_review"
    assert london_row["stale"] == 1
    assert london_row["id"] in [f["id"] for f in s2.review_queue()]
