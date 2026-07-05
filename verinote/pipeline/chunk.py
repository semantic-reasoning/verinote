# SPDX-License-Identifier: MPL-2.0
"""Deterministic source-text chunking for resumable extraction."""

from __future__ import annotations

import re


DEFAULT_CHUNK_CHARS = 300
DEFAULT_OVERLAP_CHARS = 40


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Split source text into stable chunks with light adjacent overlap."""
    text = text.strip()
    if not text:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    overlap_chars = max(0, min(overlap_chars, max_chars // 2))

    chunks = _base_chunks(text, max_chars=max_chars)
    if not overlap_chars or len(chunks) < 2:
        return chunks

    with_overlap = [chunks[0]]
    for previous, chunk in zip(chunks, chunks[1:]):
        overlap = previous[-overlap_chars:].strip()
        with_overlap.append(f"{overlap}\n\n{chunk}" if overlap else chunk)
    return with_overlap


def _base_chunks(text: str, *, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_text(paragraph, max_chars=max_chars))
            continue

        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _split_long_text(text: str, *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + max_chars].strip())
        start += max_chars
    return [chunk for chunk in chunks if chunk]
