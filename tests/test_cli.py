# SPDX-License-Identifier: MPL-2.0
import verinote.cli as cli
from verinote.engine import DEFAULT_POLICY
from verinote.llm.base import ExtractedFact, LLMError
from verinote.store import Store


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

    rc = cli.main(["query", "What is Ada?"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "translated 1 question(s)" in out
    s = Store(tmp_path / "kb.sqlite")
    assert s.questions()[0]["status"] == "translated"
    assert (tmp_path / "facts" / "query.dl").is_file()


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
    qid = s.add_question("Where was Ada born?")
    s.set_question_query(qid, 'review_required("Where was Ada born?")', "review_required")
    s.close()

    rc = cli.main(["repair"])

    assert rc == 0
    assert "repaired 1/1" in capsys.readouterr().out
    assert Store(tmp_path / "kb.sqlite").questions()[0]["status"] == "translated"


def test_repair_no_review_required_errors(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    rc = cli.main(["repair"])
    assert rc == 1
    assert "no review_required questions" in capsys.readouterr().err
