# SPDX-License-Identifier: MPL-2.0
"""Run the DuckDB-backed logic check against the KB policy."""

from __future__ import annotations

from pathlib import Path

from verinote.engine import CheckReport
from verinote.store import Store

# Per-KB policy location, relative to the KB root (the db file's directory).
POLICY_RELPATH = Path("policy") / "logic-policy.dl"


def policy_path(store: Store) -> Path:
    return store.db_path.parent / POLICY_RELPATH


def load_policy(store: Store) -> str | None:
    """Read the KB's policy file, or None to fall back to the shipped default."""
    path = policy_path(store)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def verify(store: Store) -> CheckReport:
    """Run confirmed/accepted facts through the deterministic DuckDB check."""
    from verinote.pipeline.query import load_query
    from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError

    try:
        rows = store.engine_fact_terms()
    except DuckDBFactTermStoreError as exc:
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"backend: DuckDB\n\npolicy/engine error: {exc}",
            findings=[f"ERROR engine error: {exc}"],
        )
    return store.inference_cache.run_check(
        rows, policy_dl=load_policy(store), query_dl=load_query(store)
    )
