# SPDX-License-Identifier: MPL-2.0
"""DuckDB storage representation for logical terms.

DuckDB has no recursive `TERM` type, so verinote stores terms as canonical JSON
text in `VARCHAR` columns. Equality in DuckDB is then ordinary text equality,
but the text is a typed tree encoding rather than user-facing Datalog syntax:
atoms, strings, variables, numbers, and compounds cannot collide.

This module deliberately does not import DuckDB; callers can use these helpers
with any in-memory connection that has `VARCHAR` columns.
"""

from __future__ import annotations

import json
import re
from typing import Any

from verinote.engine.datalog import Declaration
from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    Term,
    Var,
    term_compare_key,
)

# Re-exported for the backend, which reads storage encoding and equality from
# one place. `term_compare_key` itself belongs to `engine.terms`: it is the term
# language's equality, not this module's storage encoding, and non-DuckDB
# callers (`pipeline.report_trace`) must be able to ask what the engine calls
# equal without importing a storage backend.
__all__ = [
    "DUCKDB_TERM_SQL_TYPE",
    "DuckDBTermError",
    "create_decl_table_sql",
    "create_relation_table_sql",
    "create_term_table_sql",
    "duckdb_value_to_term",
    "term_compare_key",
    "term_eq_sql",
    "term_to_duckdb_value",
]

DUCKDB_TERM_SQL_TYPE = "VARCHAR"

_TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DuckDBTermError(ValueError):
    """Raised when a DuckDB term value is malformed or non-canonical."""


def term_to_duckdb_value(term: Term) -> str:
    """Return the canonical DuckDB `VARCHAR` value for a logical term."""
    return json.dumps(
        _term_to_payload(term),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def duckdb_value_to_term(value: object) -> Term:
    """Decode a canonical DuckDB term value back into a logical term."""
    if not isinstance(value, str):
        raise DuckDBTermError(f"DuckDB term value must be a string, got {type(value)!r}")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DuckDBTermError(f"invalid DuckDB term JSON: {exc}") from exc
    term = _payload_to_term(payload)
    if term_to_duckdb_value(term) != value:
        raise DuckDBTermError("DuckDB term value is not canonical")
    return term


def term_eq_sql(left_expr: str, right_expr: str) -> str:
    """Return SQL comparing two physical DuckDB term expressions."""
    return f"{left_expr} = {right_expr}"


def create_term_table_sql(table_name: str, columns: tuple[str, ...]) -> str:
    """Return SQL for a table whose columns are logical terms stored as VARCHAR."""
    _validate_sql_identifier(table_name, "table")
    if not columns:
        raise ValueError("term table must have at least one column")
    column_sql: list[str] = []
    seen: set[str] = set()
    for column in columns:
        _validate_sql_identifier(column, "column")
        if column in seen:
            raise ValueError(f"duplicate column: {column}")
        seen.add(column)
        column_sql.append(
            f"{_quote_sql_identifier(column)} {DUCKDB_TERM_SQL_TYPE} NOT NULL"
        )
    return f"CREATE TABLE {_quote_sql_identifier(table_name)} (" + ", ".join(column_sql) + ")"


def create_relation_table_sql(table_name: str = "relation") -> str:
    """Return SQL for the base relation(subject, rel, object) term table."""
    return create_term_table_sql(table_name, ("subject", "rel", "object"))


def create_decl_table_sql(declaration: Declaration) -> str:
    """Return SQL for a declared Datalog predicate table."""
    return create_term_table_sql(
        declaration.name, tuple(column.name for column in declaration.columns)
    )


def _term_to_payload(term: Term) -> dict[str, Any]:
    if isinstance(term, Atom):
        return {"t": "atom", "v": term.name}
    if isinstance(term, Var):
        return {"t": "var", "v": term.name}
    if isinstance(term, StringLit):
        return {"t": "string", "v": term.value}
    if isinstance(term, NumberLit):
        return {"t": "number", "v": term.value}
    if isinstance(term, Compound):
        return {
            "a": [_term_to_payload(arg) for arg in term.args],
            "f": term.functor,
            "t": "compound",
        }
    raise TypeError(f"not a term: {term!r}")


def _payload_to_term(payload: Any) -> Term:
    if not isinstance(payload, dict):
        raise DuckDBTermError("DuckDB term payload must be an object")
    tag = payload.get("t")
    if tag == "atom":
        _require_keys(payload, {"t", "v"})
        if not isinstance(payload["v"], str):
            raise DuckDBTermError("atom payload value must be a string")
        try:
            return Atom(payload["v"])
        except ValueError as exc:
            raise DuckDBTermError(str(exc)) from exc
    if tag == "var":
        _require_keys(payload, {"t", "v"})
        if not isinstance(payload["v"], str):
            raise DuckDBTermError("var payload value must be a string")
        try:
            return Var(payload["v"])
        except ValueError as exc:
            raise DuckDBTermError(str(exc)) from exc
    if tag == "string":
        _require_keys(payload, {"t", "v"})
        if not isinstance(payload["v"], str):
            raise DuckDBTermError("string payload value must be a string")
        return StringLit(payload["v"])
    if tag == "number":
        _require_keys(payload, {"t", "v"})
        if isinstance(payload["v"], bool) or not isinstance(payload["v"], int):
            raise DuckDBTermError("number payload value must be an integer")
        return NumberLit(payload["v"])
    if tag == "compound":
        _require_keys(payload, {"a", "f", "t"})
        if not isinstance(payload["f"], str):
            raise DuckDBTermError("compound functor must be a string")
        if not isinstance(payload["a"], list):
            raise DuckDBTermError("compound args must be a list")
        args = tuple(_payload_to_term(arg) for arg in payload["a"])
        try:
            return Compound(payload["f"], args)
        except ValueError as exc:
            raise DuckDBTermError(str(exc)) from exc
    raise DuckDBTermError(f"unsupported DuckDB term tag: {tag!r}")


def _require_keys(payload: dict[str, Any], keys: set[str]) -> None:
    actual = set(payload)
    if actual != keys:
        raise DuckDBTermError(
            "DuckDB term payload has unexpected keys: "
            + ", ".join(sorted(actual.symmetric_difference(keys)))
        )


def _validate_sql_identifier(identifier: str, kind: str) -> None:
    if not _TABLE_RE.fullmatch(identifier):
        raise ValueError(f"invalid {kind} identifier: {identifier!r}")


def _quote_sql_identifier(identifier: str) -> str:
    _validate_sql_identifier(identifier, "SQL")
    return f'"{identifier}"'
