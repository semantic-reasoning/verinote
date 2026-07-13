# SPDX-License-Identifier: MPL-2.0
import shutil
import sys

import pytest

pytest.importorskip("duckdb")  # DuckDB is core; skip only in broken/minimal envs.

from verinote.store import Store  # noqa: E402
from verinote.store.analytics import compute  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402


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


def test_compute_relation_aggregates_use_sqlite_display_mirror(tmp_path):
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    s.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="confirmed",
    )

    a = compute(tmp_path / "kb.sqlite")
    s.close()

    assert dict(a.by_relation)["has_role"] == 1


# A KB path is data, not SQL syntax. ATTACH takes no prepared parameters, so the
# path is spliced into the statement text and every char below used to be read as
# syntax: an apostrophe closed the literal and killed /analytics with a
# ParserException. DuckDB's sqlite reader is happy with '?' (unlike its own
# native storage, see test_duckdb_fact_terms), so analytics must not reject it.
@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows forbids these in filenames")
@pytest.mark.parametrize("dirname", ["it's a kb", "semi;dir", "hash#dir", "weird?dir"])
def test_compute_reads_a_kb_under_a_path_that_looks_like_sql(tmp_path, dirname):
    # Seed in a plain directory, then read it from the awkward one, so this test
    # pins the ATTACH path only and not the native-storage sidecar limits.
    plain = tmp_path / "plain"
    plain.mkdir()
    s = Store(plain / "kb.sqlite")
    s.init_schema()
    s.add_fact("A", "is_a", "B", status="confirmed", confidence=0.95)
    s.close()

    awkward = tmp_path / dirname
    awkward.mkdir()
    shutil.copy(plain / "kb.sqlite", awkward / "kb.sqlite")

    a = compute(awkward / "kb.sqlite")

    assert a.available is True
    assert dict(a.by_status) == {"confirmed": 1}


def test_compute_survives_an_apostrophe_that_would_close_the_sql_literal(tmp_path):
    # The sharp end of the above: an unescaped ' terminates the ATTACH literal
    # and the rest of the path is parsed as SQL.
    awkward = tmp_path / "quote'dir"
    awkward.mkdir()
    s = Store(awkward / "kb.sqlite")
    s.init_schema()
    s.close()

    assert compute(awkward / "kb.sqlite").by_status == []
