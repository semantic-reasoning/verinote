# SPDX-License-Identifier: MPL-2.0
"""DuckDB storage for structural logical fact terms.

SQLite owns source, run, review, provenance, and audit metadata. This module owns
only the structural term payloads for fact triples, keyed by SQLite `facts.id`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from verinote.engine.duckdb_terms import (
    DuckDBTermError,
    duckdb_value_to_term,
    term_to_duckdb_value,
)
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Term, Var

FACT_TERMS_FILENAME = "facts.duckdb"

_TERM_COLUMNS = ("subject", "rel", "object")


class DuckDBFactTermStoreError(ValueError):
    """Raised when the DuckDB fact-term store cannot complete an operation."""


class DuckDBFactTermStore:
    """Low-level DuckDB store for logical fact terms keyed by SQLite fact ids."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = _connect(self.path)
        self.init_schema()

    @classmethod
    def for_root(cls, root: str | Path) -> "DuckDBFactTermStore":
        """Open the default fact-term store under a KB root."""
        return cls(fact_terms_path(root))

    def init_schema(self) -> None:
        """Create the fact term table if it does not already exist."""
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS fact_terms (
                fact_id BIGINT PRIMARY KEY,
                subject VARCHAR NOT NULL,
                rel VARCHAR NOT NULL,
                object VARCHAR NOT NULL
            )
            """
        )

    def close(self) -> None:
        """Close the owned DuckDB connection. Safe to call more than once."""
        if self._con is not None:
            self._con.close()
            self._con = None

    def put_fact_terms(
        self,
        fact_id: int,
        subject: object,
        relation: object,
        obj: object,
    ) -> None:
        """Upsert one structural fact triple for a SQLite fact id."""
        fid = _validate_fact_id(fact_id)
        values = (
            term_to_duckdb_value(_coerce_term(subject)),
            term_to_duckdb_value(_coerce_term(relation)),
            term_to_duckdb_value(_coerce_term(obj)),
        )
        self._execute(
            """
            INSERT INTO fact_terms (fact_id, subject, rel, object)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                subject = excluded.subject,
                rel = excluded.rel,
                object = excluded.object
            """,
            [fid, *values],
        )

    def get_fact_terms(self, fact_id: int) -> tuple[Term, Term, Term] | None:
        """Return one stored fact triple, or None when the fact id is absent."""
        fid = _validate_fact_id(fact_id)
        row = self._execute(
            "SELECT subject, rel, object FROM fact_terms WHERE fact_id = ?", [fid]
        ).fetchone()
        if row is None:
            return None
        return _decode_row(fid, row)

    def get_many_fact_terms(
        self, fact_ids: Iterable[int]
    ) -> dict[int, tuple[Term, Term, Term]]:
        """Return stored triples for found fact ids."""
        ids = sorted({_validate_fact_id(fact_id) for fact_id in fact_ids})
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self._execute(
            "SELECT fact_id, subject, rel, object "
            f"FROM fact_terms WHERE fact_id IN ({placeholders}) ORDER BY fact_id",
            ids,
        ).fetchall()
        return {
            int(row[0]): _decode_row(int(row[0]), (row[1], row[2], row[3]))
            for row in rows
        }

    def delete_fact_terms(self, fact_id: int) -> None:
        """Delete a fact term row. Missing ids are ignored."""
        fid = _validate_fact_id(fact_id)
        self._execute("DELETE FROM fact_terms WHERE fact_id = ?", [fid])

    def _execute(self, sql: str, params: list[object] | None = None):
        if self._con is None:
            raise DuckDBFactTermStoreError("DuckDB fact-term store is closed")
        try:
            return self._con.execute(sql, params or [])
        except DuckDBFactTermStoreError:
            raise
        except Exception as exc:
            raise DuckDBFactTermStoreError(f"DuckDB fact-term store error: {exc}") from exc


def fact_terms_path(root: str | Path) -> Path:
    """Return the default DuckDB fact-term store path for a KB root."""
    return Path(root).expanduser() / FACT_TERMS_FILENAME


def _connect(path: Path | None):
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - covered by import monkeypatch
        raise DuckDBFactTermStoreError("DuckDB is not installed") from exc

    try:
        return duckdb.connect(str(path) if path is not None else ":memory:")
    except Exception as exc:
        raise DuckDBFactTermStoreError(f"failed to open DuckDB fact-term store: {exc}") from exc


def _validate_fact_id(fact_id: int) -> int:
    if isinstance(fact_id, bool) or not isinstance(fact_id, int):
        raise DuckDBFactTermStoreError(f"fact_id must be a positive integer: {fact_id!r}")
    if fact_id <= 0:
        raise DuckDBFactTermStoreError(f"fact_id must be a positive integer: {fact_id!r}")
    return fact_id


def _coerce_term(value: object) -> Term:
    if isinstance(value, (Atom, Compound, NumberLit, StringLit, Var)):
        return value
    return StringLit(str(value))


def _decode_row(fact_id: int, row: tuple[object, object, object]) -> tuple[Term, Term, Term]:
    decoded: list[Term] = []
    for column, value in zip(_TERM_COLUMNS, row, strict=True):
        try:
            decoded.append(duckdb_value_to_term(value))
        except DuckDBTermError as exc:
            raise DuckDBFactTermStoreError(
                f"malformed DuckDB term for fact_id={fact_id} column={column}: {exc}"
            ) from exc
    return (decoded[0], decoded[1], decoded[2])
