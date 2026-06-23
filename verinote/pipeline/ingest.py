# SPDX-License-Identifier: MPL-2.0
"""Source ingestion: register text sources, convert binaries (docx/pdf) to text.

Extraction reads text, but real sources are often docx/pdf. This turns a file
into a text source under the KB so the extractor — and the coverage view — can
use it. Text files are stored as-is (`kind='text'`); a binary is run through a
per-extension converter and stored as a `.txt` alongside, with the registered
source marked `kind='conversion'`.

The converter table is open: `register_converter('.ext', fn)` adds a format.
The built-in docx/pdf converters import their (optional) library lazily and
raise a helpful `IngestError` when it is missing — install with
`pip install verinote[ingest]`.
"""

from __future__ import annotations

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
    """Resolve raw upload bytes to ``(text, kind)``; kind is ``text``/``conversion``.

    Raises `IngestError` for unsupported suffixes or conversion failures.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        try:
            return raw.decode("utf-8"), "text"
        except UnicodeDecodeError as e:
            raise IngestError("file is not valid UTF-8 text") from e
    if suffix in _CONVERTERS:
        return _CONVERTERS[suffix](raw), "conversion"
    raise IngestError(f"unsupported source type: {suffix or filename!r}")


def store_source(store: Store, root: Path, filename: str, text: str, kind: str) -> str:
    """Write `text` under `<root>/sources/` and register it. Returns the citation."""
    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    name = Path(filename).name
    out_name = name if kind == "text" else f"{Path(name).stem}.txt"
    (sources_dir / out_name).write_text(text, encoding="utf-8")
    citation = f"sources/{out_name}"
    store.add_source(citation, kind=kind)
    return citation


def ingest_file(store: Store, src: Path, *, root: Path) -> dict:
    """Convert/register a file on disk. Returns {citation, text, kind}."""
    src = Path(src)
    if not src.is_file():
        raise IngestError(f"no such file: {src}")
    text, kind = ingest_bytes(src.read_bytes(), src.name)
    citation = store_source(store, root, src.name, text, kind)
    return {"citation": citation, "text": text, "kind": kind}
