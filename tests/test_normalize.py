# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.normalize import normalize_for_extraction


def test_normalize_for_extraction_separates_person_then_role_with_evidence():
    text = "발표· 성명대표| 샘플조직"

    normalized = normalize_for_extraction(text)

    assert "성명 대표 (원문: 성명대표)" in normalized


def test_normalize_for_extraction_separates_role_then_person_with_evidence():
    text = "대표성명(전샘플회사) · CTO이름"

    normalized = normalize_for_extraction(text)

    assert "대표 성명 (원문: 대표성명)" in normalized
    assert "CTO 이름 (원문: CTO이름)" in normalized
