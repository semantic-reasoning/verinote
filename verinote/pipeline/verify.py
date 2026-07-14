# SPDX-License-Identifier: MPL-2.0
"""Run the DuckDB-backed logic check against the KB policy.

Policy resolution lives in `pipeline.policy_state`; this module only turns the
three resolved states into a `CheckReport`. The one invariant worth stating: a
report may never claim the KB is consistent without saying which policy produced
that claim — a lost policy is an error, and an absent-and-never-recorded one is
at minimum a warning.
"""

from __future__ import annotations

from verinote.engine import NO_FINDINGS_TEXT, CheckReport
from verinote.pipeline.corroboration import CorroborationPolicyError
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.pipeline.policy_state import (
    POLICY_RELPATH,
    POLICY_UNRECORDED_BANNER,
    POLICY_UNRECORDED_FINDING,
    POLICY_UNRECORDED_NO_FINDINGS_TEXT,
    PolicyMissingError,
    PolicyState,
    PolicyStatus,
    policy_missing_message,
    policy_path,
    resolve_policy,
)
from verinote.store import Store

__all__ = [
    "POLICY_RELPATH",
    "PolicyMissingError",
    "PolicyState",
    "PolicyStatus",
    "load_policy",
    "policy_path",
    "resolve_policy",
    "verify",
]


def load_policy(store: Store) -> str | None:
    """The KB's policy text, or None to use the shipped default.

    Raises `PolicyMissingError` when the KB recorded a policy file that is now
    gone: returning None there would silently substitute the shipped default for
    rules a human wrote. Every policy consumer (verification and the
    corroboration/acceptance gates alike) goes through here, so none of them can
    quietly disagree about what this KB's rules are.
    """
    state = resolve_policy(store)
    if state.status is PolicyStatus.MISSING_RECORDED:
        raise PolicyMissingError(policy_missing_message(state))
    if state.status is PolicyStatus.PRESENT:
        return state.text
    return None


def verify(store: Store) -> CheckReport:
    """Run confirmed/accepted facts through the deterministic DuckDB check."""
    from verinote.pipeline.query import load_query
    from verinote.store.duckdb_fact_terms import DuckDBFactTermStoreError

    state = resolve_policy(store)
    if state.status is PolicyStatus.MISSING_RECORDED:
        message = policy_missing_message(state)
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"backend: DuckDB\n\npolicy error: {message}",
            findings=[f"ERROR policy_missing: {message}"],
        )

    try:
        rows = engine_relation_rows(store)
    except DuckDBFactTermStoreError as exc:
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"backend: DuckDB\n\npolicy/engine error: {exc}",
            findings=[f"ERROR engine error: {exc}"],
        )
    except CorroborationPolicyError as exc:
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"backend: DuckDB\n\npolicy/error: {exc}",
            findings=[f"ERROR policy error: {exc}"],
        )
    try:
        query_dl = load_query(store)
    except CorroborationPolicyError as exc:
        return CheckReport(
            ok=False,
            errors=1,
            warnings=0,
            text=f"backend: DuckDB\n\npolicy/error: {exc}",
            findings=[f"ERROR policy error: {exc}"],
        )

    report = store.inference_cache.run_check(
        rows, policy_dl=state.text, query_dl=query_dl
    )
    if state.status is PolicyStatus.UNRECORDED_DEFAULT:
        return _with_unrecorded_policy_warning(report)
    return report


def _with_unrecorded_policy_warning(report: CheckReport) -> CheckReport:
    """Make a default-policy run unable to read as a clean bill of health.

    A banner is not enough: the engine's "no findings — knowledge base is
    consistent." sentence is a claim about *this KB's rules*, and no rules of
    this KB were run. The sentence is replaced, not merely prefixed. `ok` and
    `errors` are deliberately left alone — with no marker there is no evidence
    that a policy was ever lost, and inventing an error would be inference.
    """
    report.warnings += 1
    report.findings = [POLICY_UNRECORDED_FINDING, *report.findings]
    report.text = report.text.replace(NO_FINDINGS_TEXT, POLICY_UNRECORDED_NO_FINDINGS_TEXT)
    report.text = f"{POLICY_UNRECORDED_BANNER}\n\n{report.text}"
    return report
