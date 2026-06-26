# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    TermParseError,
    Var,
    canonical_term_key,
    parse_term,
    render_term,
)


def test_parse_variables_and_atoms():
    assert parse_term("S") == Var("S")
    assert parse_term("Answer") == Var("Answer")
    assert parse_term("wirelog") == Atom("wirelog")
    assert parse_term("participant_1") == Atom("participant_1")
    assert parse_term("_anonymous") == Atom("_anonymous")


@pytest.mark.parametrize("text", ["_", "_1", "a1", "a_b2", "A1", "Camel"])
def test_identifier_edges_are_explicit(text):
    term = parse_term(text)
    if text[0].isupper():
        assert isinstance(term, Var)
    else:
        assert isinstance(term, Atom)


def test_uppercase_functor_is_rejected():
    with pytest.raises(TermParseError, match="compound functor"):
        parse_term("F(A)")


def test_parse_and_render_strings():
    assert parse_term('"Ada"') == StringLit("Ada")
    assert parse_term('"a\\"b"') == StringLit('a"b')
    assert parse_term('"a\\\\b"') == StringLit("a\\b")
    assert parse_term('"a\\nb\\tc"') == StringLit("a\nb\tc")
    assert render_term(StringLit('a"\\b\n')) == '"a\\"\\\\b\\n"'


def test_parse_integer_numbers():
    assert parse_term("2020") == NumberLit(2020)
    assert parse_term("-1") == NumberLit(-1)
    assert render_term(NumberLit(-42)) == "-42"


@pytest.mark.parametrize(
    "text",
    ["1.5", "1.0", "1e3", "+1", "01", "--1", "1abc", "١", "²", "a١"],
)
def test_float_and_malformed_numbers_are_rejected(text):
    with pytest.raises(TermParseError):
        parse_term(text)


def test_parse_compound_terms():
    assert parse_term('person("Ada")') == Compound("person", (StringLit("Ada"),))
    assert parse_term("date(2020, 1, 1)") == Compound(
        "date", (NumberLit(2020), NumberLit(1), NumberLit(1))
    )
    assert parse_term('grant("NSF", "123")') == Compound(
        "grant", (StringLit("NSF"), StringLit("123"))
    )
    assert parse_term('role(person("Ada"), "PI")') == Compound(
        "role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))
    )
    assert parse_term("outer(inner(A), B)") == Compound(
        "outer", (Compound("inner", (Var("A"),)), Var("B"))
    )


def test_zero_arity_compound_is_supported():
    assert parse_term("unit()") == Compound("unit", ())
    assert render_term(parse_term("unit()")) == "unit()"


def test_render_canonicalizes_whitespace():
    assert render_term(parse_term(' f( A , "x" , inner( B ) ) ')) == 'f(A, "x", inner(B))'


@pytest.mark.parametrize(
    "term",
    [
        Var("Answer"),
        Atom("participant"),
        StringLit('a"b\\c'),
        NumberLit(2020),
        Compound("person", (StringLit("Ada"),)),
        Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
        Compound("unit", ()),
    ],
)
def test_render_round_trips(term):
    assert parse_term(render_term(term)) == term


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ('f("A")', "f(A)"),
        ("f(A)", "g(A)"),
        ("f(A, B)", "f(B, A)"),
        ('"f(A)"', "f(A)"),
        ("a", "A"),
        ('"a"', "a"),
        ('"1"', "1"),
    ],
)
def test_structural_inequality(left, right):
    a = parse_term(left)
    b = parse_term(right)
    assert a != b
    assert canonical_term_key(a) != canonical_term_key(b)


def test_canonical_key_is_stable_and_type_tagged():
    a = parse_term('role(person("Ada"), "PI")')
    b = parse_term(' role( person( "Ada" ) , "PI" ) ')
    assert canonical_term_key(a) == canonical_term_key(b)
    assert canonical_term_key(a) == canonical_term_key(parse_term(render_term(a)))
    assert canonical_term_key(Var("A")).startswith("V:")
    assert canonical_term_key(Atom("a")).startswith("A:")
    assert canonical_term_key(StringLit("a")).startswith("S:")
    assert canonical_term_key(NumberLit(1)).startswith("N:")
    assert canonical_term_key(Compound("f", (Var("A"),))).startswith("C:f(")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "f(",
        "f(A",
        "f(A))",
        "f(A,,B)",
        "f(,A)",
        "f(A,)",
        'f("unterminated)',
        '"unterminated',
        '"bad\\q"',
        "f(A)junk",
        "a b",
        ".decl relation(subject: symbol)",
    ],
)
def test_malformed_terms_are_rejected(text):
    with pytest.raises(TermParseError):
        parse_term(text)


def test_current_flat_relation_fields_can_be_parsed_independently():
    fields = {"subject": 'person("Ada")', "relation": "born_in", "object": '"London"'}
    assert parse_term(fields["subject"]) == Compound("person", (StringLit("Ada"),))
    assert parse_term(fields["relation"]) == Atom("born_in")
    assert parse_term(fields["object"]) == StringLit("London")
