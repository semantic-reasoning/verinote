# SPDX-License-Identifier: MPL-2.0
"""The class-vocabulary recipe `DEFAULT_POLICY` ships commented out.

The block is inactive by default, so what these tests must protect is the
*recipe*: a user who follows the instruction in the policy — uncomment every
line between BEGIN and END — has to end up with something that works. So the
tests do exactly that, programmatically, via `enabled_class_vocabulary_policy`.
Re-typing the rules here as a test-owned string would let the shipped block rot
untouched while the tests stayed green, which is the whole failure this file
exists to rule out.

Everything runs through `run_check_duckdb`. The depth ceiling is a *backend*
property — the DuckDB backend refuses recursive rules — so a parser-level test
would pass on a build whose backend was broken.

`is_a` is neither an `error_*`/`warn_*` head nor an askable relation, so it
never reaches `rep.findings` on its own. Most tests here observe it with an
`answer_q*` query, which is a test instrument and **not** how a user reaches it:
`/ask` queries may only reference `relation/3`, so `is_a` cannot be asked. The
real usage shape is a policy rule, covered by
`test_a_policy_rule_written_about_the_superclass_is_the_real_usage_shape`.
"""

from pathlib import Path

import pytest

from verinote.engine import DEFAULT_POLICY, validate_query
from verinote.engine.duckdb_backend import run_check_duckdb

_BEGIN_MARKER = "// ── BEGIN class vocabulary ──"
_END_MARKER = "// ── END class vocabulary ──"

#: Resolved from this file so the check holds in a linked worktree and in CI,
#: wherever pytest was invoked from. `tests/` sits at the repo root.
_CONFIGURATION_DOC = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"

#: The third-level remedy, one rule per derivation path. Asserted to be verbatim
#: in the policy comment and in the docs before being run, so that the advice and
#: the behaviour cannot drift apart — dropping the second line from either copy
#: is otherwise a silent one-line regression.
_THIRD_LEVEL_BY_FACT = (
    'is_a(E, T) :- relation(E, "is_a", C), subclass_of(C, S), subclass_of(S, T).'
)
_THIRD_LEVEL_BY_DOMAIN = (
    'is_a(S, T) :- relation(S, R, O), domain_of(R, C), subclass_of(C, S2),'
    " subclass_of(S2, T)."
)


def enabled_class_vocabulary_policy() -> str:
    """`DEFAULT_POLICY` with the class-vocabulary block uncommented.

    This performs the edit the policy's own instructions describe, so the thing
    under test is the shipped text rather than a copy of it. It refuses to
    produce a degenerate result: a missing marker, an unexpectedly-shaped line,
    an empty block, or a block left with no derivation rule raises instead of
    quietly returning the policy unchanged.
    Without that, deleting the block would leave every caller asserting against
    a policy with no class rules in it and passing for the wrong reason.
    """
    lines = DEFAULT_POLICY.splitlines()
    try:
        begin = lines.index(_BEGIN_MARKER)
        end = lines.index(_END_MARKER)
    except ValueError as exc:  # pragma: no cover - only on a broken policy
        raise AssertionError(
            f"DEFAULT_POLICY no longer delimits its class-vocabulary block with "
            f"{_BEGIN_MARKER!r} and {_END_MARKER!r}: {exc}"
        ) from exc
    assert begin < end, "class-vocabulary markers are in the wrong order"

    body = lines[begin + 1 : end]
    assert body, "the class-vocabulary block is empty"
    uncommented = []
    for line in body:
        if line == "//":
            uncommented.append("")
        elif line.startswith("// "):
            uncommented.append(line[3:])
        else:
            raise AssertionError(
                f"class-vocabulary block line is not a comment, so the block is "
                f"already partly live: {line!r}"
            )
    enabled = "\n".join(uncommented).strip()
    # Rules, not just a mention of `is_a`: a block reduced to its `.decl` lines
    # still contains "is_a(" and would sail past a laxer check, leaving every
    # caller asserting against a policy that derives nothing.
    assert [line for line in enabled.splitlines() if ":-" in line], (
        f"the uncommented block contains no derivation rules: {enabled!r}"
    )
    return DEFAULT_POLICY + "\n" + enabled + "\n"


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
    """Run against the policy a user gets after enabling the shipped block."""
    return run_check_duckdb(
        facts,
        policy_dl=enabled_class_vocabulary_policy() + vocabulary,
        query_dl=query or None,
    )


def test_the_shipped_policy_runs_no_class_machinery_at_all():
    """The block is inactive as shipped, and that is the point of this change.

    `DEFAULT_POLICY` is not scaffolding-only: a KB that never recorded a policy
    is verified against it at runtime, so anything live here is imposed on KBs
    whose owners never asked for it. Uncomment any rule in the shipped text and
    this goes red — `is_a` becomes a declared predicate the query can reach.
    """
    _duckdb()
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "is_a", "object": "Person"}],
        policy_dl=DEFAULT_POLICY,
        query_dl=_classes_of("Ada"),
    )

    assert rep.ok is False
    assert "unknown predicate: is_a" in rep.findings[0]


def test_enabling_the_block_is_what_turns_the_machinery_on():
    """The same KB, the same query, after following the policy's own instructions.

    Paired with the test above: together they show that the recipe is inert
    until enabled and works once enabled, which is the entire shipped contract.
    """
    _duckdb()
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Person"}],
        query=_classes_of("Ada"),
    )

    assert rep.ok is True
    assert rep.answers == ["q1: Party, Person"]


def test_direct_is_a_relation_is_derived():
    _duckdb()
    rep = _check(
        [{"subject": "Ada", "relation": "is_a", "object": "Widget"}],
        query=_classes_of("Ada"),
    )

    assert rep.ok is True
    assert rep.answers == ["q1: Widget"]


def test_one_superclass_hop_covers_both_subclasses():
    """The issue's motivating scenario, on the block's own example vocabulary.

    Person and Organization are both Party, so a rule written about Party finds
    both without enumerating them — and a third subclass is one line, not an
    edit to every rule. No vocabulary is added here on purpose: the examples
    inside the shipped block are what answer this, so an example that stopped
    working would show up as a failure rather than as dead text nobody runs.
    """
    _duckdb()
    rep = _check(
        [
            {"subject": "Ada", "relation": "is_a", "object": "Person"},
            {"subject": "Acme", "relation": "is_a", "object": "Organization"},
        ],
        query=_MEMBERS_OF_PARTY,
    )

    assert rep.answers == ["q1: Acme, Ada"]


def test_domain_of_makes_the_subject_of_a_relation_a_class_member():
    """Zed is never declared anything; using the relation is what classifies him.

    Also on the block's own example, `domain_of("hasSubscription", "Party")`.
    """
    _duckdb()
    rep = _check(
        [{"subject": "Zed", "relation": "hasSubscription", "object": "sub-1"}],
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


def test_a_policy_rule_written_about_the_superclass_is_the_real_usage_shape():
    """What the vocabulary is actually for, as opposed to how the tests observe it.

    Every other test here reads `is_a` through an `answer_q*` query, which is a
    test instrument: `validate_query` refuses a query body that references
    anything but `relation/3`, so a user cannot ask about `is_a` from the Ask
    box. The supported use is a rule in this same file, written once about the
    superclass and covering every subclass — which is the thing the issue asked
    for, so it deserves a test in the shape a user would write.
    """
    _duckdb()
    rule = (
        ".decl warn_party_without_subscription(entity: symbol)\n"
        "warn_party_without_subscription(E) :- "
        'is_a(E, "Party"), relation(E, "is_a", C), C != "Party".\n'
    )
    rep = _check(
        [
            {"subject": "Ada", "relation": "is_a", "object": "Person"},
            {"subject": "Acme", "relation": "is_a", "object": "Organization"},
        ],
        vocabulary="\n" + rule,
    )

    # One rule, written about Party alone, reached both subclasses.
    assert [f for f in rep.findings if "party_without_subscription" in f] == [
        "WARN party_without_subscription: Acme",
        "WARN party_without_subscription: Ada",
    ]


def test_is_a_cannot_be_asked_as_a_question():
    """The flip side, pinned so the docs' claim is checked rather than asserted.

    `is_a` is policy vocabulary, not query vocabulary. A reader who saw only the
    `answer_q*` queries in this file would reasonably conclude otherwise.
    """
    ok, reason = validate_query(_classes_of("Ada"))

    assert ok is False
    assert reason == "unknown predicate: is_a"


def test_the_documented_third_level_rules_are_quoted_verbatim_in_both_places():
    """The remedy is prose in two files, so the pair has to be checked as a pair.

    Dropping the second rule from either copy is a one-line edit that restores
    the asymmetry the fourth shipped rule exists to prevent, and the suite would
    not notice: the deepening test below would carry its own copy of the strings
    and keep passing. Asserting those same strings against both files is what
    ties the tested behaviour to the advice a reader is given.
    """
    assert _THIRD_LEVEL_BY_FACT in DEFAULT_POLICY
    assert _THIRD_LEVEL_BY_DOMAIN in DEFAULT_POLICY, (
        "the policy comment no longer gives the domain_of third-level rule, so it "
        "now tells a reader to deepen one path and leave the other a level short"
    )
    assert _CONFIGURATION_DOC.is_file(), f"missing {_CONFIGURATION_DOC}"
    docs = _CONFIGURATION_DOC.read_text(encoding="utf-8")
    assert _THIRD_LEVEL_BY_FACT in docs
    assert _THIRD_LEVEL_BY_DOMAIN in docs, (
        f"{_CONFIGURATION_DOC.name} no longer gives the domain_of third-level rule"
    )


def test_the_documented_third_level_rules_deepen_both_paths_together():
    """Finding: a one-rule third level deepens only the `is_a`-fact path.

    The policy comment and docs/configuration.md both say the ceiling applies to
    both paths, so the remedy they give has to lift both. Adding only the first
    rule leaves the `domain_of` path at two levels — exactly the mismatch the
    fourth shipped rule exists to prevent, reintroduced by the instructions.

    Runs the strings the test above proves are the documented ones.
    """
    _duckdb()
    both_rules = f"\n{_THIRD_LEVEL_BY_FACT}\n{_THIRD_LEVEL_BY_DOMAIN}\n"
    chain = '\nsubclass_of("Widget", "Gadget").\nsubclass_of("Gadget", "Thing").\n'
    facts = [
        {"subject": "Ada", "relation": "is_a", "object": "Widget"},
        {"subject": "Zed", "relation": "wrote", "object": "Book"},
    ]
    vocabulary = chain + '\ndomain_of("wrote", "Widget").\n' + both_rules

    by_fact = _check(facts, vocabulary=vocabulary, query=_classes_of("Ada"))
    by_domain = _check(facts, vocabulary=vocabulary, query=_classes_of("Zed"))

    assert by_fact.answers == ["q1: Gadget, Thing, Widget"]
    assert by_domain.answers == ["q1: Gadget, Thing, Widget"]


def test_enabling_the_block_produces_the_warnings_the_policy_promises():
    """The cost of switching it on, documented in the policy and in the docs.

    The `is_a` warning is why the block ships inactive: it would otherwise land
    on every KB created by `verinote init`. The `domain_of` one is why the
    example is labelled as something to replace.
    """
    _duckdb()
    rep = run_check_duckdb(
        [{"subject": "Org", "relation": "established_on", "object": "2020"}],
        policy_dl=enabled_class_vocabulary_policy(),
    )

    assert rep.ok is True
    assert rep.findings == [
        'WARN dead_rule: policy declares domain_of("hasSubscription") '
        "but no engine fact uses that relation",
        'WARN dead_rule: policy declares functional("born_on") '
        "but no engine fact uses that relation",
        'WARN dead_rule: policy declares functional("died_on") '
        "but no engine fact uses that relation",
        'WARN dead_rule: policy declares relation("is_a") '
        "but no engine fact uses that relation",
    ]
