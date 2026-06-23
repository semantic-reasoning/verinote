# SPDX-License-Identifier: Apache-2.0
import verinote.cli as cli
from verinote.llm.base import ExtractedFact, LLMError
from verinote.store import Store


def _env(monkeypatch, tmp_path):
    """Point a fresh KB at tmp_path; `cli.main` reads these via Config.load()."""
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


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
