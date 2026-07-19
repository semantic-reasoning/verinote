# SPDX-License-Identifier: MPL-2.0
"""DuckDB storage for structural logical fact terms.

SQLite owns source, run, review, provenance, and audit metadata. This module owns
only the structural term payloads for fact triples, keyed by SQLite `facts.id`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
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


@dataclass(frozen=True)
class FactTermRecord:
    """Stored logical terms plus the coherence token recorded beside them."""

    terms: tuple[Term, Term, Term]
    term_token: str | None
    content_token: str


class DuckDBFactTermStore:
    """Low-level DuckDB store for logical fact terms keyed by SQLite fact ids."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        if self.path is not None:
            _reject_unsupported_path(self.path)
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
                object VARCHAR NOT NULL,
                term_token VARCHAR
            )
            """
        )
        columns = {
            row[1]
            for row in self._execute("PRAGMA table_info('fact_terms')").fetchall()
        }
        if "term_token" not in columns:
            self._execute("ALTER TABLE fact_terms ADD COLUMN term_token VARCHAR")

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
        *,
        term_token: str | None = None,
    ) -> None:
        """Upsert one structural fact triple for a SQLite fact id."""
        fid = _validate_fact_id(fact_id)
        values = _duckdb_term_values(subject, relation, obj)
        token = term_token or fact_term_token_from_values(values)
        if token != fact_term_token_from_values(values):
            raise DuckDBFactTermStoreError("fact term token does not match term payload")
        self._execute(
            """
            INSERT INTO fact_terms (fact_id, subject, rel, object, term_token)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                subject = excluded.subject,
                rel = excluded.rel,
                object = excluded.object,
                term_token = excluded.term_token
            """,
            [fid, *values, token],
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

    def get_fact_term_record(self, fact_id: int) -> FactTermRecord | None:
        """Return one stored fact triple with its coherence token."""
        fid = _validate_fact_id(fact_id)
        row = self._execute(
            "SELECT subject, rel, object, term_token FROM fact_terms WHERE fact_id = ?",
            [fid],
        ).fetchone()
        if row is None:
            return None
        return _decode_record(fid, row)

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

    def get_many_fact_term_records(
        self, fact_ids: Iterable[int]
    ) -> dict[int, FactTermRecord]:
        """Return stored triples and coherence tokens for found fact ids."""
        ids = sorted({_validate_fact_id(fact_id) for fact_id in fact_ids})
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self._execute(
            "SELECT fact_id, subject, rel, object, term_token "
            f"FROM fact_terms WHERE fact_id IN ({placeholders}) ORDER BY fact_id",
            ids,
        ).fetchall()
        return {int(row[0]): _decode_record(int(row[0]), row[1:]) for row in rows}

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


def fact_term_token(subject: object, relation: object, obj: object) -> str:
    """Return the deterministic coherence token for one logical fact triple."""
    return fact_term_token_from_values(_duckdb_term_values(subject, relation, obj))


def fact_term_token_from_values(values: tuple[str, str, str]) -> str:
    """Return the deterministic coherence token for encoded DuckDB term values."""
    payload = "\x1f".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# DuckDB's native storage layer splits a database path on '?' and reads the tail
# as connection parameters, so a KB root like `/data/weird?dir` sends it looking
# for a file nobody named -- and it half-creates one on the way out. The split
# happens inside DuckDB, below any string we hand it, so there is nothing for us
# to quote or encode our way around: refuse the path instead of opening it.
# This is a native-storage limit only. The SQLite ATTACH in `store.analytics`
# handles '?' fine, so it stays unguarded.
_UNSUPPORTED_PATH_CHARS = ("?",)


def _reject_unsupported_path(path: Path) -> None:
    """Fail before opening a store DuckDB cannot address, naming the way out."""
    for char in _UNSUPPORTED_PATH_CHARS:
        if char in str(path):
            raise DuckDBFactTermStoreError(
                f"cannot open the DuckDB fact-term store at {str(path)!r}: DuckDB reads "
                f"everything after {char!r} in a database path as connection parameters, "
                f"not as part of the filename, so this store can never be opened. "
                f"Move the KB to a path with no {char!r} in it (rename the offending "
                f"directory, or pass a different KB root) and retry."
            )


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
    if isinstance(value, Var):
        raise DuckDBFactTermStoreError("fact terms must be ground")
    if isinstance(value, Compound):
        from verinote.store.fact_input import is_ground_term

        if not is_ground_term(value):
            raise DuckDBFactTermStoreError("fact terms must be ground")
        return value
    if isinstance(value, (Atom, NumberLit, StringLit)):
        return value
    return StringLit(str(value))


def _duckdb_term_values(subject: object, relation: object, obj: object) -> tuple[str, str, str]:
    return (
        term_to_duckdb_value(_coerce_term(subject)),
        term_to_duckdb_value(_coerce_term(relation)),
        term_to_duckdb_value(_coerce_term(obj)),
    )


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


def _decode_record(fact_id: int, row: tuple[object, object, object, object]) -> FactTermRecord:
    terms = _decode_row(fact_id, (row[0], row[1], row[2]))
    values = tuple(str(value) for value in row[:3])
    stored_token = row[3]
    if stored_token is not None and not isinstance(stored_token, str):
        raise DuckDBFactTermStoreError(
            f"malformed DuckDB term token for fact_id={fact_id}: {stored_token!r}"
        )
    return FactTermRecord(
        terms=terms,
        term_token=stored_token,
        content_token=fact_term_token_from_values(values),
    )
