# SPDX-License-Identifier: MPL-2.0
import verinote.cli as cli
from verinote.engine import DEFAULT_POLICY
from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.ingest import register_converter
from verinote.store import Store
from verinote.store.fact_input import structural_term


def _env(monkeypatch, tmp_path):
    """Point a fresh KB at tmp_path; `cli.main` reads these via Config.load()."""
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


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


def test_query_adds_and_translates(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: fake_client())
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("What is Ada?", "is_a", "Synthetic Answer", status="confirmed")
    s.close()

    rc = cli.main(["query", "What is Ada?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "translated 1 question(s)" in out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "translated"
    assert (tmp_path / "facts" / "query.dl").is_file()


def test_query_persists_translation_failure_reason(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(error=LLMError("provider unavailable")),
    )

    rc = cli.main(["query", "What is the sample answer?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: translation_failed (provider unavailable)" in out
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
    assert "q1: translation_failed (missing provider credentials)" in out
    s = Store(tmp_path / "kb.sqlite")
    q = s.questions()[0]
    assert q["status"] == "translation_failed"
    assert q["reason"] == "missing provider credentials"
    assert (tmp_path / "facts" / "query.dl").read_text(encoding="utf-8") == ""


def test_query_no_pending_errors(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    rc = cli.main(["query"])
    assert rc == 1
    assert "no pending questions" in capsys.readouterr().err


def test_repair_validates_and_translates(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            query=lambda question, qid: f'answer_q{qid}(O) :- relation("Ada", "born_in", O).'
        ),
    )
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact("Ada", "born_in", "London", status="confirmed")
    qid = s.add_question("Where was Ada born?")
    s.set_question_query(qid, 'review_required("Where was Ada born?")', "review_required")
    s.close()

    rc = cli.main(["repair"])

    assert rc == 0
    assert "repaired 1/1" in capsys.readouterr().out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "translated"


def test_repair_reports_durable_rejected_status(tmp_path, monkeypatch, capsys, fake_client):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "verinote.llm.get_client",
        lambda cfg: fake_client(
            query=lambda q, i: 'no_answer("no confirmed facts match")'
        ),
    )
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    qid = s.add_question("What is the sample answer?")
    s.set_question_query(
        qid, 'review_required("What is the sample answer?")', "review_required"
    )
    s.close()

    rc = cli.main(["repair"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "q1: kept no_answer (no confirmed facts match)" in out
    assert "repaired 0/1" in out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "no_answer"


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
