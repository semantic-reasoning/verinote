# SPDX-License-Identifier: MPL-2.0
import io

import pytest

from verinote.pipeline import ingest_bytes, ingest_file, supported_suffixes
from verinote.pipeline.ingest import IngestError
from verinote.pipeline.ingest import register_converter
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
    assert kind == "binary"
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
    assert result["kind"] == "binary"
    assert result["citation"] == "sources/report.docx"
    assert (tmp_path / "sources" / "report.docx").read_bytes() == src.read_bytes()
    assert result["artifact_path"].startswith("artifacts/sources/1/")
    assert result["artifact_path"].endswith(".txt")
    assert "converted body" in (tmp_path / result["artifact_path"]).read_text()
    assert s.sources_with_counts()[0]["kind"] == "binary"


def test_binary_sources_with_same_stem_keep_distinct_originals_and_artifacts(tmp_path):
    register_converter(".pptx", lambda raw: raw.decode("utf-8"))
    register_converter(".pdfx", lambda raw: raw.decode("utf-8"))
    s = _store(tmp_path)

    pptx = tmp_path / "report.pptx"
    pdf = tmp_path / "report.pdfx"
    pptx.write_bytes(b"slides")
    pdf.write_bytes(b"document")

    first = ingest_file(s, pptx, root=tmp_path)
    second = ingest_file(s, pdf, root=tmp_path)

    assert first["citation"] == "sources/report.pptx"
    assert second["citation"] == "sources/report.pdfx"
    assert first["artifact_path"].startswith("artifacts/sources/1/")
    assert second["artifact_path"].startswith("artifacts/sources/2/")
    assert (tmp_path / first["artifact_path"]).read_text() == "slides"
    assert (tmp_path / second["artifact_path"]).read_text() == "document"
    assert [row["path"] for row in s.sources()] == [
        "sources/report.pdfx",
        "sources/report.pptx",
    ]


def test_reingesting_same_source_keeps_previous_text_artifact(tmp_path):
    register_converter(".rtfx", lambda raw: raw.decode("utf-8"))
    s = _store(tmp_path)
    src = tmp_path / "report.rtfx"
    src.write_bytes(b"first")
    first = ingest_file(s, src, root=tmp_path)

    src.write_bytes(b"second")
    second = ingest_file(s, src, root=tmp_path)

    assert first["source_id"] == second["source_id"]
    assert first["artifact_id"] != second["artifact_id"]
    assert (tmp_path / first["artifact_path"]).read_text() == "first"
    assert (tmp_path / second["artifact_path"]).read_text() == "second"
