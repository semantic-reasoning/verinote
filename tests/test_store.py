# SPDX-License-Identifier: MPL-2.0
from verinote.engine import compile_dl
from verinote.store import ENGINE_STATUSES, Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_toggle_promotes_and_reverts(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)

    row = s.toggle_review(fid)
    assert row["status"] == "confirmed"

    row = s.toggle_review(fid)
    assert row["status"] == "needs_review"


def test_review_queue_excludes_confirmed(tmp_path):
    s = _store(tmp_path)
    s.add_fact("A", "r", "B", status="needs_review")
    s.add_fact("C", "r", "D", status="confirmed")
    queue = s.review_queue()
    assert [q["subject"] for q in queue] == ["A"]


def test_review_log_records_decision(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    rows = list(s._conn.execute("SELECT action FROM review_log"))
    assert rows[0]["action"] == "toggled"


def test_compile_dl_only_projects_triples(tmp_path):
    s = _store(tmp_path)
    # non-ASCII placeholder keeps UTF-8 round-trip coverage (not a real entity)
    s.add_fact("예시기관", "is_a", "참여기관", status="confirmed")
    s.add_fact("x", "y", "z", status="needs_review")  # must NOT appear
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert 'relation("예시기관", "is_a", "참여기관").' in dl
    assert "needs_review" not in dl
    assert '"x"' not in dl


def test_compile_dl_escapes_quotes(tmp_path):
    s = _store(tmp_path)
    s.add_fact('a"b', "r", "c", status="confirmed")
    dl = compile_dl(s.facts(statuses=ENGINE_STATUSES))
    assert r'relation("a\"b", "r", "c").' in dl


def test_amend_fact_persists_and_audits(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "is_a", "B", status="needs_review", note="orig")
    after = s.amend_fact(fid, subject="A2", relation="became", obj="C", note="fixed")
    assert (after["subject"], after["relation"], after["object"], after["note"]) == (
        "A2",
        "became",
        "C",
        "fixed",
    )
    assert [e["action"] for e in s.fact_log(fid)] == ["amended"]


def test_amend_missing_fact_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.amend_fact(999, subject="x", relation="y", obj="z") is None


def test_fact_log_orders_decisions(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")
    s.toggle_review(fid)
    s.amend_fact(fid, subject="A", relation="r", obj="B2")
    assert [e["action"] for e in s.fact_log(fid)] == ["toggled", "amended"]
