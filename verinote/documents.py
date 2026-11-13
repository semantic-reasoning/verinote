"""Bounded document decoding; originals are stored separately, unchanged."""
import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024
MAX_DOCX_XML_BYTES = 16 * 1024 * 1024


def validate_text(text):
    """Bound decoded data by UTF-8 bytes, independently of container size."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('텍스트가 없습니다. 스캔 문서는 OCR 후 등록하세요.')
    if '\x00' in text:
        raise ValueError('텍스트는 NUL 문자를 포함할 수 없습니다.')
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('추출 본문은 UTF-8 32 MiB 이하여야 합니다. 문서를 나누어 등록하세요.')
    return text


def extraction_chunks(length):
    return (length + 7999) // 8000


def aggregate_limits(source):
    """Keep old human-edit allowances; scale publication with the pinned source."""
    count = max(16, extraction_chunks(len(source)))
    return {'claims': count * 12, 'facts': count * 80}


def decode_document(filename, raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('파일은 1 byte~32 MiB여야 합니다.')
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in {'.txt', '.md', '.csv', '.json', '.rst', '.log'}:
            text = raw.decode('utf-8-sig')
        elif suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.getinfo('word/document.xml')
                limit_error = 'DOCX 본문 XML은 압축 해제 후 16 MiB까지 지원합니다. 문서를 나누어 등록하세요.'
                if info.file_size > MAX_DOCX_XML_BYTES:
                    raise ValueError(limit_error)
                # Only the body is needed; never decompress images or attachments.
                try:
                    with archive.open(info) as source:
                        xml = source.read(MAX_DOCX_XML_BYTES + 1)
                except (RuntimeError, EOFError, zlib.error) as exc:
                    raise ValueError('DOCX 본문을 읽을 수 없습니다. 암호화·압축 방식이나 파일 손상을 확인하세요.') from exc
                if len(xml) > MAX_DOCX_XML_BYTES:
                    raise ValueError(limit_error)
                if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
                    raise ValueError('DOCX XML 엔티티는 허용하지 않습니다.')
                root = ET.fromstring(xml)
                ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                def paragraph(node):
                    return ''.join('\t' if n.tag == ns + 'tab' else '\n' if n.tag in {ns + 'br', ns + 'cr'} else (n.text or '') if n.tag == ns + 't' else '' for n in node.iter())
                text = '\n'.join(paragraph(n) for n in root.iter(ns + 'p'))
        elif suffix == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError('암호화 PDF는 지원하지 않습니다.')
            if len(reader.pages) > 200:
                raise ValueError('PDF는 200쪽까지 지원합니다.')
            parts: list[str] = []
            text_bytes = 0
            for page in reader.pages:
                part = page.extract_text() or ''
                text_bytes += len(part.encode('utf-8')) + bool(parts)
                if text_bytes > MAX_TEXT_BYTES:
                    raise ValueError('추출 본문은 UTF-8 32 MiB 이하여야 합니다. 문서를 나누어 등록하세요.')
                parts.append(part)
            text = '\n'.join(parts)
        else:
            raise ValueError('지원 형식: TXT, MD, CSV, JSON, RST, LOG, PDF, DOCX')
    except (UnicodeError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError('문서 형식이 손상되었거나 UTF-8이 아닙니다.') from exc
    return validate_text(text)


def chunks(text, size=8000):
    for start in range(0, len(text), size):
        yield start, text[start:start + size]


def bounded_spans(text, size, *, minimum=None, first=False):
    """Exact contiguous slices; prefer boundaries in the latter half of a window."""
    minimum = size // 2 if minimum is None else minimum
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            matches = list(re.finditer(r'\n|[.!?。！？](?:[ \t]|$)', text[start + minimum:end]))
            if matches:
                end = start + minimum + matches[0 if first else -1].end()
        yield start, text[start:end]
        start = end
