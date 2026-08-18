# SPDX-License-Identifier: MPL-2.0
"""The shipped policy's class vocabulary, exercised through the DuckDB backend.

`subclass_of`/`domain_of` are declared by a KB and `is_a` is derived from them,
so what matters is what the *engine* derives, not what the parser accepts. The
depth ceiling these rules live under is a backend property — the DuckDB backend
refuses recursive rules — and a parser-level test would pass on a build whose
backend was broken. Every test here therefore goes through `run_check_duckdb`.

`is_a` is neither an `error_*`/`warn_*` head nor a query, so it never reaches
`rep.findings`; it is observed with a query, the way `/report` observes answers.
A KB declares its vocabulary by editing its copy of the policy, so appending the
facts to `DEFAULT_POLICY` as text is the faithful setup.
"""

import pytest

from verinote.engine import DEFAULT_POLICY
from verinote.engine.duckdb_backend import run_check_duckdb


def _duckdb():
    return pytest.importorskip("duckdb")


def _classes_of(entity: str) -> str:
    """A query asking which classes `entity` is derived to belong to."""
    return (
        ".decl answer_q1(value: symbol)\n"
        f'answer_q1(C) :- is_a("{entity}", C).\n'
    )


#: Members of a class, asked the other way round: the issue's motivating shape,
#: one rule written about the superclass covering every subclass.
_MEMBERS_OF_PARTY = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(E) :- is_a(E, "Party").\n'
)


def _check(facts, vocabulary: str = "", query: str = ""):
    return run_check_duckdb(
        facts, policy_dl=DEFAULT_POLICY + vocabulary, query_dl=query or None
    )


def test_shipped_policy_alone_derives_no_class_the_kb_did_not_state():
    """The shipped policy ships every example commented out, and that is load-bearing.

    `DEFAULT_POLICY` is not scaffolding-only: an un-migrated KB gets it at
    runtime. A live `subclass_of("Person", "Party")` would make this KB — which
    says only that Ada is a Person — derive that Ada is a Party, a class nobody
    declared. Un-commenting either example turns this red.
    """
    _duckdb()
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Person"}],
        query=_classes_of("Ada"),
    )

    assert rep.answers == ["q1: Person"]


def test_direct_is_a_relation_is_derived():
    _duckdb()
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Widget"}],
        query=_classes_of("Ada"),
    )

    assert rep.ok is True
    assert rep.answers == ["q1: Widget"]


def test_one_superclass_hop_covers_both_subclasses():
    """The issue's motivating scenario, verbatim.

    Person and Organization are both Party, so a rule written about Party finds
    both without enumerating them — and a third subclass is one line, not an
    edit to every rule.
    """
    _duckdb()
    rep = _check(
        [
            {"subject": "Ada", "relation": "is_a", "object": "Person"},
            {"subject": "Acme", "relation": "is_a", "object": "Organization"},
        ],
        vocabulary=(
            '\nsubclass_of("Person", "Party").\n'
            'subclass_of("Organization", "Party").\n'
        ),
        query=_MEMBERS_OF_PARTY,
    )

    assert rep.answers == ["q1: Acme, Ada"]


def test_domain_of_makes_the_subject_of_a_relation_a_class_member():
    """Zed is never declared anything; using the relation is what classifies him."""
    _duckdb()
    rep = _check(
        [{"subject": "Zed", "relation": "hasSubscription", "object": "sub-1"}],
        vocabulary='\ndomain_of("hasSubscription", "Party").\n',
        query=_classes_of("Zed"),
    )

    assert rep.answers == ["q1: Party"]


def test_domain_of_reaches_the_superclass_too():
    """Both derivation paths must stop at the same depth, not one short.

    Without the fourth rule this returns only Widget: a subject classified
    through `domain_of` would silently miss the superclass that a subject
    classified through an `is_a` fact reaches. Two paths, two ceilings, no
    warning — the failure shape the vocabulary exists to prevent.
    """
    _duckdb()
    rep = _check(
        [{"subject": "Zed", "relation": "wrote", "object": "Book"}],
        vocabulary=(
            '\nsubclass_of("Widget", "Thing").\ndomain_of("wrote", "Widget").\n'
        ),
        query=_classes_of("Zed"),
    )

    assert rep.answers == ["q1: Thing, Widget"]


def test_two_levels_derive_and_the_third_does_not():
    """The ceiling, stated as an exact answer list.

    Widget -> Gadget -> Thing is three levels; the shipped rules spell out one
    superclass hop, so Thing is absent. Membership assertions would let a future
    accidental deepening pass unnoticed, so this pins the whole list.
    """
    _duckdb()
    vocabulary = '\nsubclass_of("Widget", "Gadget").\nsubclass_of("Gadget", "Thing").\n'
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Widget"}],
        vocabulary=vocabulary,
        query=_classes_of("Ada"),
    )

    assert rep.answers == ["q1: Gadget, Widget"]


def test_the_domain_of_path_stops_at_the_same_level():
    """One ceiling, not two — the number in the policy comment and the docs."""
    _duckdb()
    rep = _check(
        [{"subject": "Zed", "relation": "wrote", "object": "Book"}],
        vocabulary=(
            '\nsubclass_of("Widget", "Gadget").\n'
            'subclass_of("Gadget", "Thing").\n'
            'domain_of("wrote", "Widget").\n'
        ),
        query=_classes_of("Zed"),
    )

    assert rep.answers == ["q1: Gadget, Widget"]


def test_a_transitive_is_a_rule_is_refused_by_the_backend():
    """Why the rules are written flat instead of as a transitive closure.

    A reader who does not know the backend refuses recursion would take the
    fixed depth for an arbitrary choice and "fix" it here.
    """
    _duckdb()
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Widget"}],
        vocabulary="\nis_a(E, S) :- is_a(E, C), subclass_of(C, S).\n",
        query=_classes_of("Ada"),
    )

    assert rep.ok is False
    assert rep.errors == 1
    assert len(rep.findings) == 1
    assert "recursive rules are not supported" in rep.findings[0]
