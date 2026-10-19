"""Bounded document decoding; originals are stored separately, unchanged."""
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_TEXT = 128_000


def decode_document(filename, raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('파일은 1 byte~32 MiB여야 합니다.')
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in {'.txt', '.md', '.csv', '.json', '.rst', '.log'}:
            text = raw.decode('utf-8-sig')
        elif suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 16 * 1024 * 1024:
                    raise ValueError('DOCX 압축 해제 크기는 총 16 MiB까지 지원합니다.')
                xml = archive.read('word/document.xml')
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
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or '')
                if sum(map(len, parts)) > MAX_TEXT:
                    raise ValueError('추출 본문은 128,000자까지 분석할 수 있습니다. 문서를 나누어 등록하세요.')
            text = '\n'.join(parts)
        else:
            raise ValueError('지원 형식: TXT, MD, CSV, JSON, RST, LOG, PDF, DOCX')
    except (UnicodeError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError('문서 형식이 손상되었거나 UTF-8이 아닙니다.') from exc
    if not text.strip():
        raise ValueError('텍스트가 없습니다. 스캔 문서는 OCR 후 등록하세요.')
    if len(text) > MAX_TEXT or '\x00' in text:
        raise ValueError('텍스트는 128,000자 이내이며 NUL 문자를 포함할 수 없습니다.')
    return text


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
