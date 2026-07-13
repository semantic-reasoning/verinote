# SPDX-License-Identifier: MPL-2.0
from verinote.engine import coverage
from verinote.store import Store, db
from verinote.store.fact_input import structural_term


def _engine_facts_for_source(store: Store, source_id: int) -> int:
    """Count the facts the engine itself would read for one source.

    Deliberately routed through the same status constant the engine reads
    (`db.ENGINE_STATUSES`, looked up at call time) rather than a literal, so the
    assertions below fix an invariant instead of a snapshot number.
    """
    rows = store.facts(statuses=db.ENGINE_STATUSES)
    return len([r for r in rows if r["source_id"] == source_id])


def _source_coverage(store: Store, tmp_path, path: str):
    return next(s for s in coverage(store, root=tmp_path).sources if s.path == path)


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


def test_coverage_engine_tier_follows_engine_statuses(tmp_path, monkeypatch):
    """Coverage must derive its engine tier from ENGINE_STATUSES, not from SQL.

    A source whose only facts are `superseded` is a gap today. Widen
    ENGINE_STATUSES to include `superseded` and the very same source must stop
    being a gap — because the engine would now read those facts. Hard-coding
    `IN ('confirmed','accepted')` back into the query makes this test fail.
    """
    s = _store(tmp_path)
    path = _with_file(tmp_path, "a.txt")
    sid = s.add_source(path)
    s.add_fact("A", "is_a", "B", status="superseded", source_id=sid)
    s.add_fact("C", "is_a", "D", status="superseded", source_id=sid)
    superseded_count = 2

    before = _source_coverage(s, tmp_path, path)
    assert before.engine_facts == 0
    assert before.is_gap
    assert before.total_facts == superseded_count

    monkeypatch.setattr(
        db, "ENGINE_STATUSES", db.ENGINE_STATUSES | {"superseded"}
    )

    after = _source_coverage(s, tmp_path, path)
    assert after.engine_facts == superseded_count
    assert not after.is_gap
    assert after.total_facts == superseded_count


def test_coverage_engine_facts_match_the_engines_own_input(tmp_path, monkeypatch):
    """The number coverage reports == the number of facts the engine reads.

    Asserted against the engine's own input path (`facts(statuses=...)`) rather
    than a literal, and re-checked under a mutated ENGINE_STATUSES so the two
    definitions cannot drift apart silently.
    """
    s = _store(tmp_path)
    path = _with_file(tmp_path, "a.txt")
    sid = s.add_source(path)
    s.add_fact("A", "is_a", "B", status="confirmed", source_id=sid)
    s.add_fact("C", "is_a", "D", status="accepted", source_id=sid)
    s.add_fact("E", "is_a", "F", status="needs_review", source_id=sid)
    s.add_fact("G", "is_a", "H", status="superseded", source_id=sid)

    default_cov = _source_coverage(s, tmp_path, path)
    assert default_cov.engine_facts == _engine_facts_for_source(s, sid)

    monkeypatch.setattr(
        db, "ENGINE_STATUSES", db.ENGINE_STATUSES | {"superseded"}
    )

    mutated_cov = _source_coverage(s, tmp_path, path)
    assert mutated_cov.engine_facts == _engine_facts_for_source(s, sid)
    # The invariant held both times *and* the tier actually moved, so the
    # equality above is not vacuous.
    assert mutated_cov.engine_facts == default_cov.engine_facts + 1
