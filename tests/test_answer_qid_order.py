# SPDX-License-Identifier: MPL-2.0
"""Answer buckets are ordered by question number, not by how the digits sort.

Every ordering guard here uses more than nine questions on purpose: with q1..q9
string order and numeric order agree, so a smaller fixture would pass against
the very bug this file locks.
"""

import pytest

from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.wirelog import answer_bucket_sort_key, compile_dl, run_check

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
#
# `12abc` is here for the anchor: a qid that *starts* numeric is the only shape an
# unanchored `[0-9]+` match still calls numeric, and `int("12abc")` then raises.
# All-alphabetic qids like `foo` never reach that branch, so they cannot cover it.
_NON_NUMERIC_QIDS = ["foo", "12abc"]
_NON_NUMERIC_POLICY = _RELATION_DECL + _query_for(_NON_NUMERIC_QIDS)
_NON_NUMERIC_ANSWERS = ["q12abc: London", "qfoo: London"]


def test_duckdb_keeps_a_non_numeric_answer_predicate_clean():
    _duckdb()
    rep = run_check_duckdb(_FACTS, policy_dl=_NON_NUMERIC_POLICY)

    assert rep.ok is True
    assert rep.answers == _NON_NUMERIC_ANSWERS


def test_wirelog_keeps_a_non_numeric_answer_predicate_clean():
    _pyrewire()
    rep = _wirelog_check(policy_dl=_NON_NUMERIC_POLICY)

    assert rep.ok is True
    assert rep.answers == _NON_NUMERIC_ANSWERS


_MIXED_POLICY = _RELATION_DECL + _query_for(
    list(range(1, 13)) + ["foo", "bar", "12abc"]
)
_MIXED_ORDER = [f"q{n}" for n in range(1, 13)] + ["q12abc", "qbar", "qfoo"]


def test_duckdb_sorts_numeric_answers_before_non_numeric_ones():
    _duckdb()
    rep = run_check_duckdb(_FACTS, policy_dl=_MIXED_POLICY)

    assert _qids_in_order(rep) == _MIXED_ORDER


def test_wirelog_sorts_numeric_answers_before_non_numeric_ones():
    _pyrewire()
    rep = _wirelog_check(policy_dl=_MIXED_POLICY)

    assert _qids_in_order(rep) == _MIXED_ORDER


# `answer_bucket_sort_key` calls `int(qid)` for every all-digit qid, but `int()`
# refuses a string longer than `sys.get_int_max_str_digits()` (4300 by default)
# and raises ValueError. A qid is user-authored, so an `answer_q<thousands of
# digits>` predicate reaches the sort: it has to land in the trailing bucket like
# any malformed qid, not blow up the whole answer ordering. 5000 clears 4300.
_OVERLONG_NUMERIC_QID = "1" * 5000


def test_overlong_numeric_qid_sorts_last_instead_of_raising():
    key = answer_bucket_sort_key(_OVERLONG_NUMERIC_QID)

    assert key[0] == answer_bucket_sort_key("12abc")[0]
    assert key[0] > answer_bucket_sort_key("9999")[0]


def test_answer_bucket_sort_key_orders_small_numbers_numerically():
    assert answer_bucket_sort_key("2")[1] < answer_bucket_sort_key("10")[1]
