# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.query import query_path
from verinote.pipeline.report_trace import report_trace
from verinote.store import Store


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

    assert trace.excluded_candidate_count == 1
    assert len(trace.answers) == 1
    answer = trace.answers[0]
    assert (answer.qid, answer.value, answer.conflicted) == ("1", "Sample City", False)
    assert [fact.id for fact in answer.facts] == [fact_id]
    assert answer.facts[0].source == "sources/sample.txt"
    assert answer.facts[0].evidence == "Sample Person was born in Sample City."


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
