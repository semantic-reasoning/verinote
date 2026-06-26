# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.engine import DEFAULT_POLICY
from verinote.engine.datalog import (
    AtomExpr,
    Column,
    Comparison,
    Declaration,
    DatalogParseError,
    DatalogValidationError,
    Fact,
    Rule,
    parse_and_validate_program,
    parse_program,
    validate_program,
)
from verinote.engine.terms import Atom, Compound, StringLit, Var


def test_parse_and_validate_default_policy():
    program = parse_and_validate_program(DEFAULT_POLICY)

    assert [decl.name for decl in program.declarations] == [
        "relation",
        "functional",
        "error_functional_conflict",
    ]
    assert len(program.facts) == 3
    assert program.facts[0] == Fact(
        AtomExpr("functional", (StringLit("established_on"),))
    )
    assert len(program.rules) == 1
    rule = program.rules[0]
    assert rule.head == AtomExpr("error_functional_conflict", (Var("S"), Var("R")))
    assert isinstance(rule.body[-1], Comparison)
    assert rule.body[-1].op == "!="


def test_parse_and_validate_generated_answer_query_with_relation_decl():
    source = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O).\n'
    )

    program = parse_and_validate_program(source)

    assert program.rules == (
        Rule(
            AtomExpr("answer_q1", (Var("O"),)),
            (AtomExpr("relation", (StringLit("Ada"), StringLit("born_in"), Var("O"))),),
        ),
    )


def test_comments_do_not_strip_string_content():
    source = (
        "// full-line comment\n"
        ".decl relation(subject: symbol, rel: symbol, object: symbol) // trailing\n"
        'relation("Ada // not comment", "born_in", "London").\n'
    )

    program = parse_and_validate_program(source)

    assert program.facts == (
        Fact(
            AtomExpr(
                "relation",
                (
                    StringLit("Ada // not comment"),
                    StringLit("born_in"),
                    StringLit("London"),
                ),
            )
        ),
    )


def test_nested_compound_terms_are_not_treated_as_predicates():
    source = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl answer_q2(value: symbol)\n"
        'answer_q2(person(O)) :- relation(person("Ada"), born_in, O).\n'
    )

    program = parse_and_validate_program(source)
    rule = program.rules[0]

    assert rule.head.args == (Compound("person", (Var("O"),)),)
    assert rule.body == (
        AtomExpr(
            "relation",
            (
                Compound("person", (StringLit("Ada"),)),
                Atom("born_in"),
                Var("O"),
            ),
        ),
    )


def test_zero_arity_declarations_and_facts_are_supported():
    program = parse_and_validate_program(".decl ready()\nready().")

    assert program.declarations == (Declaration("ready", ()),)
    assert program.facts == (Fact(AtomExpr("ready", ())),)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ('p("x").', "unknown predicate: p"),
        ('.decl p(x: symbol)\np("a", "b").', "arity mismatch for p"),
        ('.decl p(x: symbol)\n.decl q(x: symbol)\np(X) :- q(Y).', "unsafe head"),
        (
            '.decl p(x: symbol)\n.decl q(x: symbol)\np(X) :- q(X), Y != "blocked".',
            "unsafe comparison",
        ),
        ('.decl p(x: symbol)\np(X) :- X == "Ada".', "unsafe head"),
        (".decl p(x: string)", "unsupported type"),
        (".decl p(x: symbol)\n.decl p(x: symbol)", "duplicate declaration"),
        ('.decl p(x: symbol)\np(X).', "fact contains variable"),
    ],
)
def test_validation_rejects_invalid_programs(source, message):
    program = parse_program(source)
    with pytest.raises(DatalogValidationError, match=message):
        validate_program(program)


@pytest.mark.parametrize(
    "source",
    [
        ".input p",
        ".declp(x: symbol)",
        ".decl p(x symbol)",
        '.decl p(x: symbol) p("a").',
        ".decl p(x: symbol)\np(X) :- not q(X).",
        ".decl p(x: symbol)\np(X) :- X < 3.",
        ".decl p(x: symbol)\np(X) :- q(X),.",
        ".decl p(x: symbol)\np(X) :- q(X)",
        ".decl p(x: symbol)\np(X",
        ".decl p(x: symbol)\np(X). junk",
    ],
)
def test_parse_rejects_unsupported_syntax(source):
    with pytest.raises(DatalogParseError):
        parse_program(source)


def test_parse_rejects_comparison_with_missing_side():
    with pytest.raises(DatalogParseError, match="requires two terms"):
        parse_program(".decl p(x: symbol)\np(X) :- X != .")


def test_declarations_preserve_columns_and_types():
    program = parse_program(".decl relation(subject: symbol, rel: symbol)")

    assert program.declarations == (
        Declaration(
            "relation",
            (Column("subject", "symbol"), Column("rel", "symbol")),
        ),
    )
