# SPDX-License-Identifier: MPL-2.0
"""Dead-rule detection: a policy relation no engine fact uses is a non-blocking note.

These unit tests target the pure `dead_rule_warnings` helper, so they run in CI
without the optional pyrewire engine installed.
"""

from pathlib import Path

import pytest

from verinote.engine import DEFAULT_POLICY, compile_dl, run_check
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.wirelog import dead_rule_warnings
from verinote.pipeline.query_candidate_eval import RELATION_DECL
from verinote.pipeline.verify import policy_path, verify
from verinote.store import Store


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


# --- The shipped class vocabulary (#537) --------------------------------------
# The class-vocabulary block ships commented out, so from the detector's point of
# view the shipped policy is unchanged. These pin that it stayed that way. What
# happens once a KB enables the block lives in tests/test_policy_class_vocabulary.py,
# next to the helper that performs the enabling.

_DEAD_FUNCTIONAL = [
    'dead_rule: policy declares functional("born_on") '
    "but no engine fact uses that relation",
    'dead_rule: policy declares functional("died_on") '
    "but no engine fact uses that relation",
    'dead_rule: policy declares functional("established_on") '
    "but no engine fact uses that relation",
]

#: A minimal policy whose one dead rule is the `is_a` one — used to ask the
#: engine what it prints instead of restating it.
_IS_A_PROBE_POLICY = (
    ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
    ".decl is_a(entity: symbol, cls: symbol)\n"
    'is_a(E, C) :- relation(E, "is_a", C).\n'
)

#: `_DEAD_FUNCTIONAL` above stays a literal on purpose: it is what pins the
#: detector's wording for the *shipped* policy. The wording is not unguarded
#: either way — three tests below spell the full string out inline
#: (`test_a_live_domain_of_is_flagged_because_its_column_is_named_rel`,
#: `test_rule_body_string_literal_relation_is_detected`,
#: `test_dead_body_relation_still_detected_alongside_a_live_finding_head`), and
#: three other files pin it as literals (test_duckdb_backend,
#: test_inference_cache, test_policy_class_vocabulary) — but this is the file a
#: reader opens to find out what the detector says, so the check belongs here too.

#: Resolved from this file so the check holds in a linked worktree and in CI,
#: wherever pytest was invoked from. `tests/` sits at the repo root.
_CONFIGURATION_DOC = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"


def test_the_shipped_policy_warns_about_nothing_it_ships():
    """A KB that uses every relation the shipped policy names sees a clean report.

    This is the promise the policy `verinote init` scaffolds makes. Adding class
    vocabulary to the *live* policy would break it — every KB with facts but no
    `is_a` fact would carry a dead-rule warning, this one included — so this is
    the guard that keeps the block commented out.
    """
    assert dead_rule_warnings(DEFAULT_POLICY, {"established_on", "born_on", "died_on"}) == []


def test_the_shipped_policy_names_no_relation_beyond_the_functional_three():
    """The full warning list for a KB that uses none of them.

    Exact equality rather than "`is_a` is absent": a negative substring check
    also passes when the detector is broken and returns nothing at all.
    """
    assert dead_rule_warnings(DEFAULT_POLICY, {"unrelated"}) == _DEAD_FUNCTIONAL


def test_the_is_a_warning_is_quoted_verbatim_where_it_is_explained():
    """The warning text is a three-way contract, so all three corners are checked.

    Two places *explain* this warning: the policy comment tells a reader how to
    clear it, and docs/configuration.md documents it as expected output. Both
    quote it verbatim, so both rot silently when the detector is reworded.

    The expected string comes from `dead_rule_warnings` itself, which is what
    makes this catch that. Reword the detector and it follows, leaving the two
    prose copies behind — here, not somewhere a developer can satisfy by editing
    a different assertion.

    It is derived inside the test rather than at module scope on purpose. At
    module scope an empty result aborts collection, which takes down every
    assertion in this file — the ones that pin the detector's wording — and hides
    the rest of the session's failures behind a collection error. Here the file
    still collects, the other tests still run and still report, and this one
    failure names its own cause.

    A missing docs file fails rather than skips: a guard that quietly stops
    running is the failure it was written to prevent.
    """
    warnings = dead_rule_warnings(_IS_A_PROBE_POLICY, {"born_on"})
    assert warnings, (
        "the probe policy no longer produces a dead-rule warning, so dead-rule "
        "detection is broken or its inputs changed"
    )
    dead_is_a = warnings[0]

    assert dead_is_a in DEFAULT_POLICY, (
        "the shipped policy no longer quotes the warning it tells the reader to "
        "expect; update the DEFAULT_POLICY comment in verinote/engine/wirelog.py"
    )
    assert _CONFIGURATION_DOC.is_file(), f"missing {_CONFIGURATION_DOC}"
    assert dead_is_a in _CONFIGURATION_DOC.read_text(encoding="utf-8"), (
        f"{_CONFIGURATION_DOC.name} no longer quotes the warning it documents as "
        "expected output; update the 'Logic policy vocabulary' section"
    )


def test_a_live_domain_of_is_flagged_because_its_column_is_named_rel():
    """`domain_of`'s first column is `rel`, so the detector reaches it.

    Written against a standalone policy rather than the shipped one: the class
    vocabulary is commented out in `DEFAULT_POLICY`, and this is a fact about
    the detector, not about what verinote ships.
    """
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl domain_of(rel: symbol, cls: symbol)\n"
        ".decl is_a(entity: symbol, cls: symbol)\n"
        'domain_of("hasSubscription", "Party").\n'
        "is_a(S, C) :- relation(S, R, O), domain_of(R, C).\n"
    )

    assert dead_rule_warnings(policy, {"born_on"}) == [
        'dead_rule: policy declares domain_of("hasSubscription") '
        "but no engine fact uses that relation"
    ]


def test_a_live_subclass_of_is_invisible_to_the_detector():
    """`subclass_of` names classes, not relations, so nothing here can flag it.

    A characterization test: it records what the detector does today, not what it
    ought to do. An unused or misspelled `subclass_of` is silent, and a user has
    to be told that because nothing will tell them at runtime.

    IF YOU MADE THIS RED by teaching the detector to read class columns, deleting
    this test is the correct repair — not reverting your change. The blind spot
    is a limitation being recorded, not a behaviour anything depends on. The
    matching warning in docs/configuration.md should go at the same time.
    """
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl subclass_of(sub: symbol, super: symbol)\n"
        ".decl is_a(entity: symbol, cls: symbol)\n"
        'subclass_of("Nope", "Nada").\n'
        'is_a(E, S) :- relation(E, "is_a", C), subclass_of(C, S).\n'
    )

    assert dead_rule_warnings(policy, {"is_a"}) == []


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


def test_finding_head_payload_is_not_a_dependency():
    # A finding head's literal is the payload the rule *emits*, not a relation it
    # reads. This rule fires whenever an is_a fact exists, so flagging it is a
    # false positive. Reporting relation findings as (subject, rel) is a natural
    # shape for a custom policy, so this must stay clean.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_required(subject: symbol, rel: symbol)\n"
        'error_required(S, "required_relation") :- relation(S, "is_a", "thing").\n'
    )
    assert dead_rule_warnings(policy, {"is_a"}) == []


def test_derived_head_payload_is_not_a_dependency():
    # Same for a non-finding intermediate: a rule head is output either way.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl tagged(subject: symbol, rel: symbol)\n"
        ".decl error_x(subject: symbol)\n"
        'tagged(S, "audited") :- relation(S, "is_a", "thing").\n'
        "error_x(S) :- tagged(S, _).\n"
    )
    assert dead_rule_warnings(policy, {"is_a"}) == []


def test_dead_body_relation_still_detected_alongside_a_live_finding_head():
    # The head payload is ignored, but the body's unused relation is still dead.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_required(subject: symbol, rel: symbol)\n"
        'error_required(S, "required_relation") :- relation(S, "must_have", O).\n'
    )
    assert dead_rule_warnings(policy, {"is_a"}) == [
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
    rep = run_check(dl, policy_dl=DEFAULT_POLICY)
    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings >= 1
    assert any("WARN dead_rule" in f for f in rep.findings)


# --- Production path (DuckDB) -------------------------------------------------
# The tests above exercise the pure helper; these exercise the wiring #286 adds
# to `duckdb_backend._collect_report`, the path every real report goes through.


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_duckdb_production_path_surfaces_dead_rule_with_consistent_count():
    """The production report path now emits the dead-rule WARN #245 could not.

    This is the regression the issue asks for, and unlike the pyrewire-gated
    legacy test above it actually runs in CI. It also pins the three-locations
    count invariant: the `warnings: N` baked into the report body must match the
    `warnings` field exactly, or the merge updated the WARN line but not the
    count.
    """
    pytest.importorskip("duckdb")
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ],
        policy_dl=DEFAULT_POLICY,
    )

    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings >= 1
    assert any("WARN dead_rule" in f for f in rep.findings)
    assert f"warnings: {rep.warnings}  facts:" in rep.text


def test_duckdb_production_path_stays_silent_on_a_kb_that_uses_every_decl():
    """A clean bill of health from the shipped policy, on the real report path.

    The class vocabulary added no live rule, so a KB using all three functional
    relations still reports nothing at all. Exact equality on the empty list: the
    #537 rework exists because an earlier draft put a warning here, on every KB
    that has facts but none using `is_a`. A KB with no facts yet is quiet under
    any draft -- `dead_rule_warnings` returns `[]` before it looks at the policy
    (`test_empty_fact_set_is_never_flagged` above pins that).
    """
    pytest.importorskip("duckdb")
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Ada", "relation": "born_on", "object": "1815"},
            {"subject": "Ada", "relation": "died_on", "object": "1852"},
        ],
        policy_dl=DEFAULT_POLICY,
    )

    assert rep.ok is True
    assert rep.findings == []
    assert "no findings — knowledge base is consistent." in rep.text


def test_verify_end_to_end_surfaces_dead_rule_for_recorded_policy(tmp_path):
    """The issue's headline acceptance criterion, through the real `verify()`."""
    pytest.importorskip("duckdb")
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    path = policy_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl functional(rel: symbol)\n"
        'functional("acquired_on").\n'
        ".decl error_functional_conflict(subject: symbol, rel: symbol)\n"
        "error_functional_conflict(S, R) :- "
        "relation(S, R, A), relation(S, R, B), functional(R), A != B.\n",
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings == 1
    assert any('dead_rule: policy declares functional("acquired_on")' in f for f in rep.findings)
    s.close()


def test_verify_aliased_fact_reads_canonical_relation_not_raw(tmp_path):
    """The canonical-field guard: an aliased fact must not read as a dead rule.

    "설립" is a shipped alias of `established_on`, canonicalized at read time. The
    dead-rule check reads each fact's canonical `relation`, so
    `functional("established_on")` is seen as alive. Had it read `relation_raw`
    (still "설립"), established_on would be falsely flagged dead — this test fails
    in exactly that case. born_on/died_on stay genuinely unused, proving the
    detector ran rather than silently doing nothing.
    """
    pytest.importorskip("duckdb")
    s = _store(tmp_path)
    s.add_fact("Org", "설립", "2020", status="confirmed")

    rep = verify(s)

    assert rep.ok is True
    assert rep.errors == 0
    assert not any('functional("established_on")' in f for f in rep.findings)
    assert any('dead_rule: policy declares functional("born_on")' in f for f in rep.findings)
    s.close()


def test_duckdb_zero_input_is_never_flagged_as_dead():
    """An empty KB is no engine input, not a dead policy — the guard holds here."""
    pytest.importorskip("duckdb")
    rep = run_check_duckdb([], policy_dl=DEFAULT_POLICY)

    assert rep.ok is True
    assert not any("dead_rule" in f for f in rep.findings)


def test_duckdb_rule_less_relation_decl_emits_no_dead_rule():
    """`ask`/`query_candidate_eval` pass RELATION_DECL: it must stay inert.

    A bare `relation/3` decl references no relation literal, so dead-rule
    detection is empty by construction — the query paths never start emitting
    this note even over real facts.
    """
    pytest.importorskip("duckdb")
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ],
        policy_dl=RELATION_DECL,
    )

    assert rep.ok is True
    assert not any("dead_rule" in f for f in rep.findings)


def test_duckdb_conflict_and_dead_rule_coexist_without_suppression():
    """A blocking ERROR and a non-blocking dead-rule WARN must both survive.

    This is the only shape that can catch a false-*suppression* merge bug: every
    other production-path test has `errors == 0`, so none of them could prove a
    dead-rule note fails to accidentally clear a real gate. established_on is used
    (its conflict is real and blocks); born_on/died_on are unused (dead notes).
    """
    pytest.importorskip("duckdb")
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "established_on", "object": "2021"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ],
        policy_dl=DEFAULT_POLICY,
    )

    assert rep.ok is False
    assert rep.errors >= 1
    assert rep.warnings >= 1
    assert "ERROR functional_conflict: Org established_on" in rep.findings
    assert any(f.startswith("WARN dead_rule:") for f in rep.findings)
