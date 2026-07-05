# SPDX-License-Identifier: MPL-2.0
"""Source-text normalization used only as LLM analysis input."""

from __future__ import annotations

import re


_KO_NAME = r"[가-힣]{2,5}"
_ROLE = r"대표이사|대표|CTO|CEO|CFO|담당자|발표자|총괄"
_PERSON_THEN_ROLE = re.compile(rf"(?P<person>{_KO_NAME})(?P<role>{_ROLE})(?![가-힣])")
_ROLE_THEN_PERSON = re.compile(rf"(?<![가-힣])(?P<role>{_ROLE})(?P<person>{_KO_NAME})")


def normalize_for_extraction(text: str) -> str:
    """Make compact role/name text easier to extract while preserving evidence.

    PDF text extraction often collapses Korean role expressions such as
    ``성명대표`` or ``대표성명``. The normalized text is not written back to the
    source artifact; it is only fed to extraction chunks.
    """

    def person_then_role(match: re.Match[str]) -> str:
        original = match.group(0)
        return f"{match.group('person')} {match.group('role')} (원문: {original})"

    def role_then_person(match: re.Match[str]) -> str:
        original = match.group(0)
        return f"{match.group('role')} {match.group('person')} (원문: {original})"

    normalized = _PERSON_THEN_ROLE.sub(person_then_role, text)
    return _ROLE_THEN_PERSON.sub(role_then_person, normalized)
