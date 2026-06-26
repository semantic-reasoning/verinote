# SPDX-License-Identifier: MPL-2.0
"""Read-only metadata analytics over the KB via DuckDB.

SQLite stays the system-of-record for metadata, review lifecycle, and text
display mirrors. Logical fact terms live in the DuckDB fact-term sidecar and are
used by verification, not by these aggregate counts. Analytics reuse the same
DuckDB dependency as the inference backend and ATTACH the SQLite file read-only
(the `sqlite` extension auto-loads on attach). There is no write path here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def duckdb_available() -> bool:
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Analytics:
    """Aggregates for the analytics panel. `available=False` when DuckDB is absent."""

    by_status: list[tuple[str, int]] = field(default_factory=list)
    by_relation: list[tuple[str, int]] = field(default_factory=list)  # display mirror
    by_source: list[tuple[str, int]] = field(default_factory=list)
    by_confidence: list[tuple[str, int]] = field(default_factory=list)
    available: bool = True


# Confidence buckets, low→high; the CASE mirrors these labels.
_CONFIDENCE_CASE = """
    CASE
        WHEN confidence < 0.5 THEN '0.0–0.5'
        WHEN confidence < 0.7 THEN '0.5–0.7'
        WHEN confidence < 0.9 THEN '0.7–0.9'
        ELSE '0.9–1.0'
    END
"""


def compute(db_path: Path) -> Analytics:
    """ATTACH the SQLite KB read-only and return aggregate breakdowns.

    Returns an empty, `available=False` `Analytics` when DuckDB isn't installed.
    """
    if not duckdb_available():
        return Analytics(available=False)

    import duckdb

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{db_path}' AS kb (TYPE sqlite, READ_ONLY);")
        by_status = con.execute(
            "SELECT status, COUNT(*) FROM kb.facts GROUP BY status ORDER BY COUNT(*) DESC, status"
        ).fetchall()
        by_relation = con.execute(
            "SELECT relation, COUNT(*) FROM kb.facts GROUP BY relation "
            "ORDER BY COUNT(*) DESC, relation LIMIT 20"
        ).fetchall()
        by_source = con.execute(
            "SELECT COALESCE(s.path, '(none)') AS src, COUNT(*) "
            "FROM kb.facts f LEFT JOIN kb.sources s ON s.id = f.source_id "
            "GROUP BY src ORDER BY COUNT(*) DESC, src LIMIT 20"
        ).fetchall()
        by_confidence = con.execute(
            f"SELECT {_CONFIDENCE_CASE} AS bucket, COUNT(*) "
            "FROM kb.facts GROUP BY bucket ORDER BY bucket"
        ).fetchall()
    finally:
        con.close()

    return Analytics(
        by_status=[(str(s), int(n)) for s, n in by_status],
        by_relation=[(str(r), int(n)) for r, n in by_relation],
        by_source=[(str(s), int(n)) for s, n in by_source],
        by_confidence=[(str(b), int(n)) for b, n in by_confidence],
    )
