# SPDX-License-Identifier: MPL-2.0
"""Source ingestion: register original sources and extraction text artifacts.

Extraction reads text, but facts cite original files. Text uploads are stored
under `sources/` and reused as their own extraction artifact. Binary uploads are
stored under `sources/` with their original filename, converted through a
per-extension converter, and the converted text is stored separately under
`artifacts/sources/<source_id>/`.

The converter table is open: `register_converter('.ext', fn)` adds a format.
The built-in docx/pdf converters import their (optional) library lazily and
raise a helpful `IngestError` when it is missing — install with
`pip install verinote[ingest]`.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Callable

from verinote.store import Store

# Stored as-is, no conversion.
TEXT_SUFFIXES = {".txt", ".md"}


class IngestError(RuntimeError):
    """Unsupported source type, a missing converter dependency, or a parse failure."""


Converter = Callable[[bytes], str]
_CONVERTERS: dict[str, Converter] = {}


def register_converter(suffix: str, fn: Converter) -> None:
    """Register a binary→text converter for a file extension (e.g. ``.rtf``)."""
    _CONVERTERS[suffix.lower()] = fn


def supported_suffixes() -> set[str]:
    """Every suffix the uploader/ingester accepts (text + registered binaries)."""
    return TEXT_SUFFIXES | set(_CONVERTERS)


def _convert_docx(raw: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise IngestError("docx ingestion needs `pip install verinote[ingest]`") from e
    try:
        document = docx.Document(BytesIO(raw))
    except Exception as e:  # python-docx raises various errors on bad input
        raise IngestError(f"could not read docx: {e}") from e
    return "\n".join(p.text for p in document.paragraphs)


def _convert_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise IngestError("pdf ingestion needs `pip install verinote[ingest]`") from e
    try:
        reader = PdfReader(BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        raise IngestError(f"could not read pdf: {e}") from e


register_converter(".docx", _convert_docx)
register_converter(".pdf", _convert_pdf)


def ingest_bytes(raw: bytes, filename: str) -> tuple[str, str]:
    """Resolve raw upload bytes to ``(text, kind)``; kind is ``text``/``binary``.

    Raises `IngestError` for unsupported suffixes or conversion failures.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        try:
            return raw.decode("utf-8"), "text"
        except UnicodeDecodeError as e:
            raise IngestError("file is not valid UTF-8 text") from e
    if suffix in _CONVERTERS:
        return _CONVERTERS[suffix](raw), "binary"
    raise IngestError(f"unsupported source type: {suffix or filename!r}")


def store_source(
    store: Store, root: Path, filename: str, raw: bytes, text: str, kind: str
) -> dict:
    """Persist an original source and its extraction text artifact."""
    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    name = Path(filename).name
    source_path = sources_dir / name
    source_path.write_bytes(raw)
    citation = f"sources/{name}"
    source_id = store.add_source(citation, kind=kind)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    artifact_dir = root / "artifacts" / "sources" / str(source_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{digest}.txt"
    if not artifact_path.exists():
        artifact_path.write_text(text, encoding="utf-8")
    artifact_relpath = f"artifacts/sources/{source_id}/{digest}.txt"

    artifact_id = store.add_source_artifact(
        source_id=source_id,
        kind="extracted_text",
        path=artifact_relpath,
        checksum=digest,
    )
    return {
        "citation": citation,
        "text": text,
        "kind": kind,
        "source_id": source_id,
        "artifact_id": artifact_id,
        "artifact_path": artifact_relpath,
    }


def ingest_file(store: Store, src: Path, *, root: Path) -> dict:
    """Convert/register a file on disk. Returns {citation, text, kind}."""
    src = Path(src)
    if not src.is_file():
        raise IngestError(f"no such file: {src}")
    raw = src.read_bytes()
    text, kind = ingest_bytes(raw, src.name)
    return store_source(store, root, src.name, raw, text, kind)
