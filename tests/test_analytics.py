# SPDX-License-Identifier: MPL-2.0
import pytest

pytest.importorskip("duckdb")  # analytics is an optional extra

from verinote.store import Store  # noqa: E402
from verinote.store.analytics import compute  # noqa: E402


def _seeded(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    sid = s.add_source("sources/a.txt")
    s.add_fact("A", "is_a", "B", status="confirmed", confidence=0.95, source_id=sid)
    s.add_fact("C", "is_a", "D", status="candidate", confidence=0.4, source_id=sid)
    s.add_fact("E", "born_on", "F", status="confirmed", confidence=0.8)
    return s


def test_compute_aggregates_against_attached_sqlite(tmp_path):
    s = _seeded(tmp_path)
    a = compute(tmp_path / "kb.sqlite")  # read while writer is still open (WAL)
    s.close()

    assert a.available is True
    assert dict(a.by_status) == {"confirmed": 2, "candidate": 1}
    assert dict(a.by_relation)["is_a"] == 2
    assert dict(a.by_source)["sources/a.txt"] == 2
    buckets = dict(a.by_confidence)
    assert buckets["0.9–1.0"] == 1  # 0.95
    assert buckets["0.7–0.9"] == 1  # 0.8
    assert buckets["0.0–0.5"] == 1  # 0.4
