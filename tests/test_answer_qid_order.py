# SPDX-License-Identifier: MPL-2.0
"""Answer buckets are ordered by question number, not by how the digits sort.

Every ordering guard here uses more than nine questions on purpose: with q1..q9
string order and numeric order agree, so a smaller fixture would pass against
the very bug this file locks.
"""

import pytest

from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.wirelog import compile_dl, run_check

_FACTS = [{"subject": "Ada", "relation": "born_in", "object": "London"}]
_RELATION_DECL = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"


def _duckdb():
    return pytest.importorskip("duckdb")


def _pyrewire():
    return pytest.importorskip("pyrewire")


def _query_for(qids):
    """Build query rules answering `born_in` once per question id."""
    return "".join(
        f".decl answer_q{qid}(value: symbol)\n"
        f'answer_q{qid}(O) :- relation("Ada", "born_in", O).\n'
        for qid in qids
    )


def _qids_in_order(rep):
    return [answer.split(":")[0] for answer in rep.answers]


def _wirelog_check(query_dl=None, policy_dl=None):
    return run_check(compile_dl(_FACTS), policy_dl=policy_dl, query_dl=query_dl)


def test_duckdb_answers_order_by_question_number():
    _duckdb()
    rep = run_check_duckdb(_FACTS, query_dl=_query_for(range(1, 13)))

    assert _qids_in_order(rep) == [f"q{n}" for n in range(1, 13)]


def test_duckdb_answers_order_across_the_hundreds_boundary():
    _duckdb()
    rep = run_check_duckdb(_FACTS, query_dl=_query_for([9, 10, 99, 100, 101]))

    assert _qids_in_order(rep) == ["q9", "q10", "q99", "q100", "q101"]


def test_wirelog_answers_order_by_question_number():
    _pyrewire()
    rep = _wirelog_check(query_dl=_query_for(range(1, 13)))

    assert _qids_in_order(rep) == [f"q{n}" for n in range(1, 13)]


def test_wirelog_answers_order_across_the_hundreds_boundary():
    _pyrewire()
    rep = _wirelog_check(query_dl=_query_for([9, 10, 99, 100, 101]))

    assert _qids_in_order(rep) == ["q9", "q10", "q99", "q100", "q101"]


# A policy is user-authored, so nothing stops a `answer_q<non-numeric>` relation
# from reaching the answer collectors. Ordering it must not cost the report its
# `ok` (duckdb turns the exception into an internal engine error, which flips the
# review gate) nor raise out of wirelog, which collects outside any try.
_NON_NUMERIC_POLICY = (
    _RELATION_DECL + ".decl answer_qfoo(value: symbol)\n"
    'answer_qfoo(O) :- relation("Ada", "born_in", O).\n'
)


def test_duckdb_keeps_a_non_numeric_answer_predicate_clean():
    _duckdb()
    rep = run_check_duckdb(_FACTS, policy_dl=_NON_NUMERIC_POLICY)

    assert rep.ok is True
    assert rep.answers == ["qfoo: London"]


def test_wirelog_keeps_a_non_numeric_answer_predicate_clean():
    _pyrewire()
    rep = _wirelog_check(policy_dl=_NON_NUMERIC_POLICY)

    assert rep.ok is True
    assert rep.answers == ["qfoo: London"]


_MIXED_POLICY = (
    _RELATION_DECL
    + _query_for(range(1, 13))
    + ".decl answer_qfoo(value: symbol)\n"
    'answer_qfoo(O) :- relation("Ada", "born_in", O).\n'
    ".decl answer_qbar(value: symbol)\n"
    'answer_qbar(O) :- relation("Ada", "born_in", O).\n'
)
_MIXED_ORDER = [f"q{n}" for n in range(1, 13)] + ["qbar", "qfoo"]


def test_duckdb_sorts_numeric_answers_before_non_numeric_ones():
    _duckdb()
    rep = run_check_duckdb(_FACTS, policy_dl=_MIXED_POLICY)

    assert _qids_in_order(rep) == _MIXED_ORDER


def test_wirelog_sorts_numeric_answers_before_non_numeric_ones():
    _pyrewire()
    rep = _wirelog_check(policy_dl=_MIXED_POLICY)

    assert _qids_in_order(rep) == _MIXED_ORDER
