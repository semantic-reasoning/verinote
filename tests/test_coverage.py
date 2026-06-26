# SPDX-License-Identifier: MPL-2.0
from verinote.engine import coverage
from verinote.store import Store
from verinote.store.fact_input import structural_term


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _with_file(tmp_path, name: str) -> str:
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / name).write_text("body", encoding="utf-8")
    return f"sources/{name}"


def test_confirmed_source_is_covered(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source(_with_file(tmp_path, "a.txt"))
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)
    sc = coverage(s, root=tmp_path).sources[0]
    assert sc.engine_facts == 1 and not sc.is_gap and not sc.is_orphan


def test_structural_confirmed_fact_uses_sqlite_metadata_for_coverage(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source(_with_file(tmp_path, "a.txt"))
    s.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="confirmed",
        source_id=sid,
    )

    sc = coverage(s, root=tmp_path).sources[0]

    assert sc.engine_facts == 1
    assert sc.total_facts == 1
    assert not sc.is_gap


def test_needs_review_only_is_a_gap(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source(_with_file(tmp_path, "a.txt"))
    s.add_fact("A", "is_a", "B", status="needs_review", source_id=sid)
    cov = coverage(s, root=tmp_path)
    sc = cov.sources[0]
    assert sc.engine_facts == 0 and sc.total_facts == 1 and sc.is_gap
    assert cov.covered == []


def test_zero_fact_source_is_a_gap(tmp_path):
    s = _store(tmp_path)
    s.add_source(_with_file(tmp_path, "a.txt"))
    sc = coverage(s, root=tmp_path).sources[0]
    assert sc.total_facts == 0 and sc.is_gap


def test_orphan_when_backing_file_missing(tmp_path):
    s = _store(tmp_path)
    sid = s.add_source("sources/missing.txt")  # no file written
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)
    cov = coverage(s, root=tmp_path)
    sc = cov.sources[0]
    assert sc.is_orphan is True
    assert sc in cov.orphans
