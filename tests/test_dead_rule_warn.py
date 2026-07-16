# SPDX-License-Identifier: MPL-2.0
"""Dead-rule detection: a policy relation no engine fact uses is a non-blocking note.

These unit tests target the pure `dead_rule_warnings` helper, so they run in CI
without the optional pyrewire engine installed.
"""

import pytest

from verinote.engine import DEFAULT_POLICY, compile_dl, run_check
from verinote.engine.wirelog import dead_rule_warnings


def test_default_policy_flags_unused_functional_relations():
    present = {"established_on", "is_a"}
    warnings = dead_rule_warnings(DEFAULT_POLICY, present)
    joined = "\n".join(warnings)
    # born_on / died_on are declared functional but no fact uses them.
    assert any('functional("born_on")' in w for w in warnings)
    assert any('functional("died_on")' in w for w in warnings)
    # established_on IS used by a fact, so it is not a dead rule.
    assert 'functional("established_on")' not in joined
    assert all(w.startswith("dead_rule: policy declares ") for w in warnings)


def test_no_warnings_when_every_referenced_relation_is_present():
    present = {"established_on", "born_on", "died_on"}
    assert dead_rule_warnings(DEFAULT_POLICY, present) == []


def test_empty_fact_set_is_never_flagged():
    # An empty KB is not a dead policy — it is simply no engine input yet.
    assert dead_rule_warnings(DEFAULT_POLICY, set()) == []


def test_rule_body_string_literal_relation_is_detected():
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_x(subject: symbol)\n"
        'error_x(S) :- relation(S, "must_have", O).\n'
    )
    warnings = dead_rule_warnings(policy, {"is_a"})
    assert warnings == [
        'dead_rule: policy declares relation("must_have") '
        "but no engine fact uses that relation"
    ]


def test_malformed_policy_does_not_crash():
    assert dead_rule_warnings("this is not valid datalog !!!", {"is_a"}) == []


def test_query_relations_are_never_treated_as_dead_rules():
    # dead_rule_warnings only inspects the policy; a query is a user question.
    query = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O).\n'
    )
    assert dead_rule_warnings(query, {"is_a"}) == []


def test_run_check_surfaces_dead_rule_as_nonblocking_finding():
    pytest.importorskip("pyrewire")
    # A KB that uses established_on but never born_on/died_on: no conflict, but
    # the default policy's born_on/died_on functional decls are dead rules.
    dl = compile_dl(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ]
    )
    rep = run_check(dl)
    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings >= 1
    assert any("WARN dead_rule" in f for f in rep.findings)
