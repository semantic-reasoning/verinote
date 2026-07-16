# SPDX-License-Identifier: MPL-2.0
"""Engine integrations and deterministic KB checks."""

from verinote.engine.coverage import Coverage, SourceCoverage, coverage
from verinote.engine.duckdb_backend import DuckDBInferenceCache, run_check_duckdb
from verinote.engine.wirelog import (
    DEFAULT_POLICY,
    NO_FINDINGS_TEXT,
    CheckReport,
    FindingRow,
    compile_dl,
    run_check,
    validate_query,
)

__all__ = [
    "compile_dl",
    "run_check",
    "run_check_duckdb",
    "DuckDBInferenceCache",
    "validate_query",
    "CheckReport",
    "FindingRow",
    "DEFAULT_POLICY",
    "NO_FINDINGS_TEXT",
    "coverage",
    "Coverage",
    "SourceCoverage",
]
