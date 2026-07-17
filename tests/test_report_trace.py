# SPDX-License-Identifier: MPL-2.0
from verinote.engine.terms import Atom
from verinote.pipeline.query import query_path
from verinote.pipeline.report_trace import report_trace
from verinote.pipeline.verify import verify
from verinote.store import Store, db as store_db


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_report_trace_links_direct_answers_to_engine_facts_and_evidence(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("sources/sample.txt")
    fact_id = s.add_fact(
        "Sample Person",
        "born_in",
        "Sample City",
        status="confirmed",
        source_id=source_id,
    )
    s.add_fact_evidence(
        fact_id=fact_id,
        source_id=source_id,
        snippet="Sample Person was born in Sample City.",
    )
    s.add_fact(
        "Candidate Person",
        "born_in",
        "Draft City",
        status="candidate",
        source_id=source_id,
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "born_in", O).\n',
        encoding="utf-8",
    )

    trace = report_trace(s)

    assert trace.excluded_review_count == 1
    assert len(trace.answers) == 1
    answer = trace.answers[0]
    assert (answer.qid, answer.value, answer.conflicted) == ("1", "Sample City", False)
    assert [fact.id for fact in answer.facts] == [fact_id]
    assert answer.facts[0].source == "sources/sample.txt"
    assert answer.facts[0].evidence == "Sample Person was born in Sample City."


def test_report_answer_and_trace_render_a_comma_value_the_same_way(tmp_path):
    """/report shows one answer twice; both renderings must agree.

    "Query answers" (`rep.answers`, rendered by the engine backend) and
    "Traceability" (`trace.answers`, rendered by report_trace) are two views of
    the same derived answer on the same page. The backend escapes a surface
    comma as `\\,` so a value cannot forge two answers across the `, ` join
    (issue #167); if the trace renders the same value its own way, the page
    shows `Analytical Engine\\, Ltd` in one section and the ambiguous
    `Analytical Engine, Ltd` in the other, and the ambiguity this PR removed is
    simply reintroduced one section lower.
    """
    s = _store(tmp_path)
    source_id = s.add_source("sources/org.txt")
    s.add_fact(
        "Ada",
        "works_at",
        "Analytical Engine, Ltd",
        status="confirmed",
        source_id=source_id,
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "works_at", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)
    trace = report_trace(s)

    # The escaped comma is what keeps this one answer from reading as two.
    assert rep.answers == ["q1: Analytical Engine\\, Ltd"]
    assert [(a.qid, a.value) for a in trace.answers] == [
        ("1", "Analytical Engine\\, Ltd")
    ]
    # ...and the two sections agree: the answer line is exactly the traced
    # values joined the way the backend joins them, so a renderer change that
    # reaches only one of the two sections fails here.
    assert rep.answers == ["q1: " + ", ".join(a.value for a in trace.answers)]


def test_report_trace_ignores_invalid_relation_alias_policy(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `role`\n", encoding="utf-8")
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "role", O).\n',
        encoding="utf-8",
    )

    trace = report_trace(s)

    assert trace.answers == ()


def test_report_trace_marks_answers_from_conflicted_relations(tmp_path):
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    first = s.add_fact(
        "Sample Org",
        "established_on",
        "2020",
        status="confirmed",
        source_id=source_a,
    )
    second = s.add_fact(
        "Sample Org",
        "established_on",
        "2021",
        status="accepted",
        source_id=source_b,
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Org", "established_on", O).\n',
        encoding="utf-8",
    )

    trace = report_trace(s)

    assert [(answer.value, answer.conflicted) for answer in trace.answers] == [
        ("2020", True),
        ("2021", True),
    ]
    assert {fact.id for answer in trace.answers for fact in answer.facts} == {
        first,
        second,
    }


def test_report_trace_skips_non_direct_query_rules(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Sample Person", "born_in", "Sample City", status="confirmed")
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("Sample Person", "born_in", O), O != "Other".\n',
        encoding="utf-8",
    )

    trace = report_trace(s)

    assert trace.answers == ()


def test_report_trace_breaks_the_excluded_count_down_by_status(tmp_path):
    """The report must name what was held back: candidate and needs_review call
    for different user action, so a bare total cannot stand in for both.
    """
    s = _store(tmp_path)
    source_id = s.add_source("sources/sample.txt")
    s.add_fact("Person", "born_in", "A", status="candidate", source_id=source_id)
    s.add_fact("Person", "born_in", "B", status="candidate", source_id=source_id)
    s.add_fact("Person", "born_in", "C", status="needs_review", source_id=source_id)

    trace = report_trace(s)

    assert trace.excluded_review_count == 3
    assert trace.excluded_by_status == (("candidate", 2), ("needs_review", 1))


def test_report_trace_omits_review_statuses_with_no_facts(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("sources/sample.txt")
    s.add_fact("Person", "born_in", "A", status="candidate", source_id=source_id)

    assert report_trace(s).excluded_by_status == (("candidate", 1),)


def test_report_trace_excluded_count_follows_review_statuses(tmp_path, monkeypatch):
    """Widen REVIEW_STATUSES at its home and this consumer must follow.

    Pinning today's number would be vacuous: both sides would be hardcoded and
    would still agree. `superseded` is a real schema status belonging to neither
    REVIEW_STATUSES nor ENGINE_STATUSES, so it serves as the mutation.
    """
    s = _store(tmp_path)
    source_id = s.add_source("sources/sample.txt")
    s.add_fact(
        "Person", "born_in", "Draft City", status="candidate", source_id=source_id
    )
    s.add_fact(
        "Person", "born_in", "Old City", status="superseded", source_id=source_id
    )

    assert report_trace(s).excluded_review_count == 1

    monkeypatch.setattr(
        store_db, "REVIEW_STATUSES", store_db.REVIEW_STATUSES | {"superseded"}
    )

    trace = report_trace(s)

    assert trace.excluded_review_count == 2
    assert trace.excluded_by_status == (("candidate", 1), ("superseded", 1))


def test_report_trace_matches_engine_equal_representation_twins(tmp_path):
    """Trace matching must use the engine's equality, not dataclass equality.

    The DuckDB backend compares body constants and variable joins on
    `term_compare_key`, so `Atom("x")` and `StringLit("x")` are one value to the
    engine (both map to `s:x`). `report_trace` matched query terms against fact
    terms with dataclass `==`, which separates the two. The result was an answer
    on `/report` with an empty Traceability row: the engine derived it, but the
    trace could name no fact behind it -- exactly the provenance the report
    exists to show.
    """
    s = _store(tmp_path)
    source_id = s.add_source("sources/twins.txt")
    fact_id = s.add_fact(
        Atom("x"),
        "role",
        "Chief",
        status="confirmed",
        source_id=source_id,
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("x", "role", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)
    trace = report_trace(s)

    # The engine answers, because `"x"` and the stored `x` are one value to it.
    assert rep.answers == ["q1: Chief"]
    # ...so the trace must name the fact behind that answer.
    assert [(a.qid, a.value) for a in trace.answers] == [("1", "Chief")]
    assert [fact.id for fact in trace.answers[0].facts] == [fact_id]


def test_report_trace_joins_a_repeated_variable_on_engine_equality(tmp_path):
    """A repeated query variable must join the way the engine joins it.

    The DuckDB backend joins a repeated variable on the compare-key column
    (`__cmp_<column> = __cmp_<column>`), so a fact whose subject is `Atom("ada")`
    and whose object is `StringLit("ada")` satisfies `relation(S, _, S)`. Trace
    matching bound `S` to the subject term and compared the object with
    dataclass `==`, so the twins never joined and the answer lost its
    provenance.
    """
    s = _store(tmp_path)
    source_id = s.add_source("sources/twins.txt")
    fact_id = s.add_fact(
        Atom("ada"),
        "same_as",
        "ada",
        status="confirmed",
        source_id=source_id,
    )
    query_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    query_path(tmp_path).write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(S) :- relation(S, "same_as", S).\n',
        encoding="utf-8",
    )

    rep = verify(s)
    trace = report_trace(s)

    assert rep.answers == ["q1: ada"]
    assert [(a.qid, a.value, tuple(f.id for f in a.facts)) for a in trace.answers] == [
        ("1", "ada", (fact_id,))
    ]
