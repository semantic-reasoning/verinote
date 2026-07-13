# SPDX-License-Identifier: MPL-2.0
from pathlib import Path

import pytest

import verinote.cli as cli
from verinote import config
from verinote.engine import DEFAULT_POLICY
from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.ingest import register_converter
from verinote.pipeline.query_intent import parse_query_intent
from verinote.store import ENGINE_STATUSES, Store
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

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translation_failed - provider unavailable" in out
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

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translation_failed - missing provider credentials" in out
    s = Store(tmp_path / "kb.sqlite")
    q = s.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "missing provider credentials"
    assert (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8") == ""


def test_query_no_pending_errors(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
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
    _env(monkeypatch, tmp_path)
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
    # Both sides must be non-empty, else the disjointness check below is vacuous.
    assert demo_statuses
    assert ENGINE_STATUSES
    assert demo_statuses & ENGINE_STATUSES == set()


def test_seeded_kb_has_no_engine_facts(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    root = tmp_path / "kb"

    assert cli.main(["init", str(root), "--seed"]) == 0

    store = Store(root / "kb.sqlite")
    assert store.facts(statuses=ENGINE_STATUSES) == []
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
