# SPDX-License-Identifier: MPL-2.0
"""Zero-review-rule detection: a policy that checks nothing is not a clean bill.

A policy file that declares `relation/3` (the engine's only loud gate) but no
`error_*`/`warn_*` rule runs clean and derives nothing, so both the web report
and `coverage --strict` would otherwise green-light a KB nothing reviews. The
pure `review_rule_count` tests below run in CI without DuckDB; the `verify()` and
query-eval tests need the DuckDB backend.

The last test is the load-bearing anti-regression guard: the check must live in
the KB-review callers (`verify()`, `coverage --strict`), never in the shared
engine, because the query-evaluation paths run a deliberately rule-less
`relation/3` policy and are correct to report `ok=True` with no warning.
"""

import pytest

from verinote.engine import DEFAULT_POLICY
from verinote.engine.wirelog import review_rule_count
from verinote.pipeline.query_candidate_eval import RELATION_DECL
from verinote.pipeline.verify import (
    NO_REVIEW_RULES_FINDING,
    policy_path,
    verify,
)
from verinote.store import Store

BARE_RELATION_POLICY = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"


# --- Pure helper (no DuckDB needed) ------------------------------------------


def test_default_policy_has_a_review_rule():
    # DEFAULT_POLICY ships `error_functional_conflict`, so it reviews something.
    assert review_rule_count(DEFAULT_POLICY) >= 1


def test_bare_relation_decl_has_no_review_rules():
    assert review_rule_count(BARE_RELATION_POLICY) == 0


def test_malformed_policy_counts_zero_review_rules():
    # Matches `dead_rule_warnings`: the parse failure is an engine error, so this
    # helper must not invent a second verdict — it just reports nothing to count.
    assert review_rule_count("this is not valid datalog !!!") == 0


def test_answer_only_policy_has_no_review_rules():
    # An `answer_q*` head answers a query; it does not review the KB.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O).\n'
    )
    assert review_rule_count(policy) == 0


def test_single_warn_rule_is_counted():
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_x(subject: symbol)\n"
        'warn_x(S) :- relation(S, "is_a", "thing").\n'
    )
    assert review_rule_count(policy) >= 1


def test_bare_declaration_without_a_rule_is_not_counted():
    # A `.decl` for an `error_*` predicate declares it but runs no check: the
    # count is over rule *heads*, not declarations.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_x(subject: symbol)\n"
    )
    assert review_rule_count(policy) == 0


# --- Production path: verify() (needs DuckDB) --------------------------------


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _write_policy(store: Store, text: str) -> None:
    path = policy_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_verify_warns_when_present_policy_has_no_review_rules(tmp_path):
    """The issue's headline: a recorded rule-less policy must not read as clean.

    `ok`/`errors` stay put — nothing inconsistent was derived — but a warning is
    added and the engine's clean-bill sentence is replaced, so the report can no
    longer be mistaken for a real all-clear.
    """
    pytest.importorskip("duckdb")
    from verinote.engine import NO_FINDINGS_TEXT

    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    _write_policy(s, BARE_RELATION_POLICY)

    rep = verify(s)

    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings == 1
    assert NO_REVIEW_RULES_FINDING in rep.findings
    assert any("no review rules" in f for f in rep.findings)
    # The false "no findings — knowledge base is consistent." claim is gone.
    assert NO_FINDINGS_TEXT not in rep.text
    s.close()


def test_verify_stays_silent_when_present_policy_reviews_something(tmp_path):
    """False-positive guard: a policy with a real rule gets no zero-rule finding."""
    pytest.importorskip("duckdb")
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    _write_policy(s, DEFAULT_POLICY)

    rep = verify(s)

    assert rep.ok is True
    assert NO_REVIEW_RULES_FINDING not in rep.findings
    assert not any("no review rules" in f for f in rep.findings)
    s.close()


# --- Anti-regression: the check must NOT reach the engine layer --------------


def test_query_eval_path_is_unaffected_by_the_zero_rule_check():
    """The central design constraint, pinned as a permanent regression guard.

    `ask` and `query_candidate_eval` drive the DuckDB engine with a rule-less
    `RELATION_DECL` policy plus a query rule, and correctly expect `ok=True` with
    no warning. If anyone ever "simplifies" this fix by moving the zero-review
    check into the shared engine (`duckdb_backend._collect_report` / wirelog
    `run_check`), this test fails — which is exactly its job.
    """
    pytest.importorskip("duckdb")
    from verinote.engine.duckdb_backend import run_check_duckdb

    query = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O).\n'
    )
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "born_in", "object": "London"}],
        policy_dl=RELATION_DECL,
        query_dl=query,
    )

    assert rep.ok is True
    assert rep.warnings == 0
    assert not any("no review rules" in f for f in rep.findings)
    assert rep.answers == ["q1: London"]
