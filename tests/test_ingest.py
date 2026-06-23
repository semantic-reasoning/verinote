# SPDX-License-Identifier: MPL-2.0
import io

import pytest

from verinote.pipeline import ingest_bytes, ingest_file, supported_suffixes
from verinote.pipeline.ingest import IngestError
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _docx_bytes(text: str) -> bytes:
    import docx

    d = docx.Document()
    for line in text.splitlines() or [""]:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_text_passthrough():
    assert ingest_bytes(b"hello", "notes.txt") == ("hello", "text")
    assert ingest_bytes(b"# title", "notes.md") == ("# title", "text")


def test_unsupported_suffix_raises():
    with pytest.raises(IngestError, match="unsupported source type"):
        ingest_bytes(b"\x00\x01", "archive.bin")


def test_non_utf8_text_raises():
    with pytest.raises(IngestError, match="UTF-8"):
        ingest_bytes(b"\xff\xfe\x00", "notes.txt")


def test_docx_converts_to_text():
    text, kind = ingest_bytes(_docx_bytes("hello world"), "report.docx")
    assert kind == "conversion"
    assert "hello world" in text


def test_supported_suffixes_includes_binaries():
    assert {".txt", ".md", ".docx", ".pdf"} <= supported_suffixes()


def test_ingest_file_registers_text_source(tmp_path):
    s = _store(tmp_path)
    src = tmp_path / "doc.txt"
    src.write_text("body", encoding="utf-8")
    result = ingest_file(s, src, root=tmp_path)
    assert result["kind"] == "text"
    assert result["citation"] == "sources/doc.txt"
    assert (tmp_path / "sources" / "doc.txt").read_text() == "body"
    rows = s.sources_with_counts()
    assert [(r["path"], r["kind"], r["fact_count"]) for r in rows] == [
        ("sources/doc.txt", "text", 0)
    ]


def test_ingest_file_converts_binary_source(tmp_path):
    s = _store(tmp_path)
    src = tmp_path / "report.docx"
    src.write_bytes(_docx_bytes("converted body"))
    result = ingest_file(s, src, root=tmp_path)
    assert result["kind"] == "conversion"
    # the produced text file lives under sources/ as .txt
    assert result["citation"] == "sources/report.txt"
    assert "converted body" in (tmp_path / "sources" / "report.txt").read_text()
    assert s.sources_with_counts()[0]["kind"] == "conversion"
