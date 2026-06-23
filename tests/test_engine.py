# SPDX-License-Identifier: MPL-2.0
import verinote.engine.wirelog as wl
from verinote.engine import DEFAULT_POLICY, compile_dl, run_check, validate_query

# A subject with two distinct objects for a functional relation is a conflict.
_CONFLICT = compile_dl(
    [
        {"subject": "Org", "relation": "established_on", "object": "2020"},
        {"subject": "Org", "relation": "established_on", "object": "2021"},
        {"subject": "Org", "relation": "is_a", "object": "company"},
    ]
)
_CONSISTENT = compile_dl(
    [
        {"subject": "Org", "relation": "established_on", "object": "2020"},
        {"subject": "Org", "relation": "is_a", "object": "company"},
    ]
)


def test_parse_relation_facts_roundtrips_escaping():
    dl = compile_dl([{"subject": 'a"b', "relation": "r", "object": "c"}])
    assert wl._parse_relation_facts(dl) == [('a"b', "r", "c")]


def test_run_check_flags_functional_conflict():
    rep = run_check(_CONFLICT)
    assert rep.engine_available is True
    assert rep.errors > 0
    assert rep.ok is False
    # finding is human-readable: names the subject and the conflicting relation
    joined = "\n".join(rep.findings)
    assert "functional_conflict" in joined
    assert "Org" in joined and "established_on" in joined


def test_run_check_consistent_is_ok():
    rep = run_check(_CONSISTENT)
    assert rep.errors == 0
    assert rep.ok is True
    assert rep.findings == []


def test_run_check_empty_kb_is_ok():
    rep = run_check("")
    assert rep.ok is True and rep.errors == 0


def test_run_check_uses_custom_policy():
    # A policy that flags *any* is_a edge as an error.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_has_isa(subject: symbol, object: symbol)\n"
        'error_has_isa(S, O) :- relation(S, "is_a", O).\n'
    )
    rep = run_check(_CONSISTENT, policy_dl=policy)
    assert rep.errors == 1
    assert "has_isa: Org company" in "\n".join(rep.findings)


def test_run_check_surfaces_policy_error():
    rep = run_check(_CONSISTENT, policy_dl="this is not valid datalog !!!")
    assert rep.ok is False
    assert rep.errors == 1
    assert any("engine error" in f for f in rep.findings)


def test_run_check_degrades_without_engine(monkeypatch):
    monkeypatch.setattr(wl, "_load_engine", lambda: None)
    rep = run_check(_CONFLICT)
    assert rep.engine_available is False
    assert rep.ok is True and rep.errors == 0  # cannot gate without the engine
    assert "not installed" in rep.text


def test_default_policy_declares_finding_relations():
    assert "error_functional_conflict" in DEFAULT_POLICY
    assert ".decl relation(" in DEFAULT_POLICY


def test_run_check_evaluates_query():
    dl = compile_dl(
        [
            {"subject": "Ada", "relation": "born_in", "object": "London"},
            {"subject": "Ada", "relation": "is_a", "object": "mathematician"},
        ]
    )
    query = '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n'
    rep = run_check(dl, query_dl=query)
    assert rep.ok is True
    assert rep.answers == ["q1: London"]
    assert "q1: London" in rep.text


def test_run_check_without_query_has_no_answers():
    dl = compile_dl([{"subject": "Ada", "relation": "is_a", "object": "x"}])
    assert run_check(dl).answers == []


def test_validate_query_accepts_relation_only():
    ok, reason = validate_query(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("a", "b", O).'
    )
    assert ok is True and reason == ""


def test_validate_query_rejects_unknown_predicate():
    ok, reason = validate_query(".decl answer_q1(value: symbol)\nanswer_q1(O) :- bogus(O).")
    assert ok is False and "bogus" in reason


def test_validate_query_rejects_syntax_error():
    ok, reason = validate_query("this is not datalog")
    assert ok is False and reason
