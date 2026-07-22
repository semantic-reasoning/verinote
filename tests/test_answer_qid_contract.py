# SPDX-License-Identifier: MPL-2.0
"""One definition of the `answer_q*` qid format, shared by every reader.

The engine collectors take any `answer_q<something>` relation, because a policy
and a query file are both user-authored and nothing gates them. When a reader
downstream re-decided that format for itself, the two definitions drifted: the
report's Traceability section matched `answer_q[0-9]+` and so dropped answers
the "Query answers" line above it had just printed, and Ask's prefix strip
matched `^q\\d+:` and so left `qfoo: ` in front of a user's answer.

Each guard here pins a *result* on both sides of one of those seams, so a reader
that goes back to deciding the format locally fails rather than diverges quietly.
"""

import pytest

from verinote.engine.wirelog import (
    answer_line,
    answer_qid,
    strip_answer_line_prefix,
    validate_query,
)
from verinote.pipeline.ask import _render_engine_answer_body
from verinote.pipeline.query import query_path
from verinote.pipeline.report_trace import report_trace
from verinote.pipeline.verify import verify
from verinote.store import Store


def _store_with_born_in(tmp_path, query_dl: str) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("sources/ada.txt")
    store.add_fact(
        "Ada", "born_in", "London", status="confirmed", source_id=source_id
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(query_dl, encoding="utf-8")
    return store


def _answer_rule(qid: str) -> str:
    return (
        f".decl answer_q{qid}(value: symbol)\n"
        f'answer_q{qid}(O) :- relation("Ada", "born_in", O).\n'
    )


# --- the helper's own contract -------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ("answer_q1", "1"),
        ("answer_q0", "0"),
        # Non-numeric qids are the whole point: the collectors have always
        # accepted them, so the shared reader has to hand them back, not filter.
        ("answer_qfoo", "foo"),
        # Starts numeric, is not a number — the shape that an unanchored
        # `[0-9]+` match would still call numeric.
        ("answer_q12abc", "12abc"),
        # A bare `answer_q` relation has an empty qid, and an empty qid is still
        # a qid. Callers must test `is None`, never truthiness.
        ("answer_q", ""),
        # The prefix test is `startswith`, exactly as both collectors have always
        # done it. Narrowing it here would silently drop a relation the engine
        # still counts as an answer and still prints.
        ("answer_query", "uery"),
        # Not answers at all.
        ("answer_x", None),
        ("answer", None),
        ("error_q1", None),
        ("warn_q1", None),
        ("relation", None),
        # The prefix is a prefix, not a substring.
        ("my_answer_q1", None),
    ],
)
def test_answer_qid_reads_the_qid_out_of_a_predicate_name(predicate, expected):
    assert answer_qid(predicate) == expected


@pytest.mark.parametrize("qid", ["0", "7", "foo", "12abc", ""])
def test_stripping_the_answer_prefix_undoes_rendering_it(qid):
    """The strip is the formatter's inverse, which is why it takes the qid."""
    assert strip_answer_line_prefix(answer_line(qid, ["London"]), qid) == "London"


def test_answer_line_joins_a_bucket_the_way_report_reads_it():
    assert answer_line("3", ["London", "Paris"]) == "q3: London, Paris"


@pytest.mark.parametrize(
    "line",
    [
        # Another question's prefix is not this question's to remove.
        "q3: London",
        # A value may legitimately open with something prefix-shaped; a pattern
        # loose enough to cover non-numeric qids would eat both of these.
        "qty: 5",
        "queue: London",
        "London",
        # A prefix is only a prefix at the front of the line.
        "born in q0: London",
    ],
)
def test_stripping_leaves_a_line_that_is_not_this_qids_answer_alone(line):
    assert strip_answer_line_prefix(line, 0) == line


def test_stripping_removes_only_the_one_prefix_it_added():
    assert strip_answer_line_prefix("q0: q0: London", 0) == "q0: London"


# --- the query contract stayed strict ------------------------------------
#
# Sharing the prefix constant with the collectors must not loosen the snippet
# contract: an LLM-authored query still has to declare a numbered question.


@pytest.mark.parametrize(
    "name",
    [
        "answer_qfoo",  # not numeric at all
        "answer_q12abc",  # starts numeric, is not a number
        "answer_q",  # no qid
        "answer_q1x",  # one stray character past the digits
    ],
)
def test_validate_query_still_requires_a_numbered_answer_predicate(name):
    pytest.importorskip("duckdb")
    ok, reason = validate_query(
        f".decl {name}(value: symbol)\n"
        f'{name}(O) :- relation("Ada", "born_in", O).'
    )

    assert ok is False
    assert "answer" in reason


def test_validate_query_accepts_a_numbered_answer_predicate():
    pytest.importorskip("duckdb")
    ok, reason = validate_query(_answer_rule("1"))

    assert (ok, reason) == (True, "")


# --- /report: the answer line and the trace below it agree ---------------


def test_report_traces_a_non_numeric_qid_the_answer_line_shows(tmp_path):
    """A qid the engine answered must not vanish from Traceability.

    A query file is user-authored, so `answer_qfoo` reaches the engine and gets
    printed under "Query answers". While the trace matched `answer_q[0-9]+` it
    skipped that rule, and /report showed an answer with no facts behind it.
    """
    pytest.importorskip("duckdb")
    store = _store_with_born_in(tmp_path, _answer_rule("foo"))

    rep = verify(store)
    trace = report_trace(store)

    assert rep.answers == ["qfoo: London"]
    assert [(a.qid, a.value) for a in trace.answers] == [("foo", "London")]
    assert [fact.subject for fact in trace.answers[0].facts] == ["Ada"]


def test_report_orders_traced_answers_the_way_the_answer_line_orders_them(tmp_path):
    """Both sides sort on the same key, so both list the questions alike.

    q2/q10 catch a string sort; `foo` catches a reader that drops or reorders
    the non-numeric bucket instead of parking it after the numbered ones.
    """
    pytest.importorskip("duckdb")
    store = _store_with_born_in(
        tmp_path, "".join(_answer_rule(qid) for qid in ("10", "2", "foo"))
    )

    rep = verify(store)
    trace = report_trace(store)

    assert rep.answers == ["q2: London", "q10: London", "qfoo: London"]
    assert [a.qid for a in trace.answers] == ["2", "10", "foo"]


# --- Ask: the report prefix never reaches the user -----------------------


def test_ask_strips_the_report_prefix_from_a_bare_engine_answer():
    """`q<id>: ` is a /report artifact; Ask shows the answer, not the artifact.

    This is the fallback rendering, used when the source trace came back empty.
    """
    assert _render_engine_answer_body(("q0: London",), ()) == "London"


def test_ask_keeps_a_value_that_merely_looks_prefixed():
    """Ask asks q0 and only q0, so nothing else on the line is a prefix.

    A pattern wide enough to also strip a non-numeric qid would take `qty: ` off
    the front of a real answer; keying on the known qid cannot.
    """
    assert _render_engine_answer_body(("q0: qty: 5",), ()) == "qty: 5"
    assert _render_engine_answer_body(("qty: 5",), ()) == "qty: 5"
