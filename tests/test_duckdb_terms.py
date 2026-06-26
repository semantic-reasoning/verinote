# SPDX-License-Identifier: MPL-2.0
import json

import pytest

from verinote.engine.datalog import Column, Declaration
from verinote.engine.duckdb_terms import (
    DUCKDB_TERM_SQL_TYPE,
    DuckDBTermError,
    create_decl_table_sql,
    create_relation_table_sql,
    create_term_table_sql,
    duckdb_value_to_term,
    term_eq_sql,
    term_to_duckdb_value,
)
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var, render_term


@pytest.mark.parametrize(
    "term",
    [
        Atom("a"),
        Var("A"),
        StringLit("a"),
        NumberLit(1),
        NumberLit(-1),
        Compound("unit", ()),
        Compound("f", (StringLit(","), StringLit(")"), Atom("a"))),
        Compound(
            "role",
            (Compound("person", (StringLit("Ada"),)), StringLit("PI")),
        ),
        StringLit('a"b\\c\n'),
    ],
)
def test_term_values_round_trip(term):
    value = term_to_duckdb_value(term)

    assert duckdb_value_to_term(value) == term
    assert render_term(duckdb_value_to_term(value)) == render_term(term)
    assert value == json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (Atom("a"), StringLit("a")),
        (Var("A"), StringLit("A")),
        (Var("A"), Atom("a")),
        (NumberLit(1), StringLit("1")),
        (Compound("f", (StringLit("A"),)), StringLit("f(A)")),
        (Compound("f", (Var("A"),)), Compound("f", (StringLit("A"),))),
    ],
)
def test_term_values_distinguish_term_types(left, right):
    assert term_to_duckdb_value(left) != term_to_duckdb_value(right)


@pytest.mark.parametrize(
    "value",
    [
        '{"t":"atom","v":"a","extra":true}',
        '{"t":"atom","v":1}',
        '{"t":"number","v":true}',
        '{"t":"number","v":1.5}',
        '{"t":"compound","f":"f","a":{}}',
        '{"t":"atom","v":"Bad"}',
        '{"t":"var","v":"bad"}',
        '{"t":"compound","f":"Bad","a":[]}',
        '{"t":"unknown","v":"a"}',
        '{"v":"a","t":"atom"}',
        "not json",
    ],
)
def test_malformed_or_noncanonical_values_are_rejected(value):
    with pytest.raises(DuckDBTermError):
        duckdb_value_to_term(value)


def test_create_relation_table_sql_uses_varchar_term_columns():
    assert create_relation_table_sql() == (
        'CREATE TABLE "relation" ('
        f'"subject" {DUCKDB_TERM_SQL_TYPE} NOT NULL, '
        f'"rel" {DUCKDB_TERM_SQL_TYPE} NOT NULL, '
        f'"object" {DUCKDB_TERM_SQL_TYPE} NOT NULL)'
    )


def test_create_decl_table_sql_uses_declared_columns():
    decl = Declaration("answer_q1", (Column("value", "symbol"),))
    assert create_decl_table_sql(decl) == (
        f'CREATE TABLE "answer_q1" ("value" {DUCKDB_TERM_SQL_TYPE} NOT NULL)'
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: create_term_table_sql("bad-name", ("value",)),
        lambda: create_term_table_sql("answer", ("bad-name",)),
        lambda: create_term_table_sql("answer", ()),
        lambda: create_term_table_sql("answer", ("value", "value")),
    ],
)
def test_create_term_table_sql_rejects_invalid_shapes(call):
    with pytest.raises(ValueError):
        call()


def _duckdb():
    return pytest.importorskip("duckdb")


def test_duckdb_round_trips_stored_terms():
    duckdb = _duckdb()
    con = duckdb.connect()
    con.execute(create_relation_table_sql())
    term = Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI")))

    con.execute(
        "INSERT INTO relation VALUES (?, ?, ?)",
        [
            term_to_duckdb_value(term),
            term_to_duckdb_value(Atom("rel")),
            term_to_duckdb_value(term),
        ],
    )

    row = con.execute("SELECT subject, object FROM relation").fetchone()
    assert duckdb_value_to_term(row[0]) == term
    assert duckdb_value_to_term(row[1]) == term


def test_duckdb_structurally_equal_nested_terms_compare_equal():
    duckdb = _duckdb()
    con = duckdb.connect()
    con.execute("CREATE TABLE terms (left_value VARCHAR NOT NULL, right_value VARCHAR NOT NULL)")
    left = Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI")))
    right = Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI")))

    con.execute(
        "INSERT INTO terms VALUES (?, ?)",
        [term_to_duckdb_value(left), term_to_duckdb_value(right)],
    )

    assert con.execute(
        f"SELECT {term_eq_sql('left_value', 'right_value')} FROM terms"
    ).fetchone() == (True,)


def test_duckdb_structurally_different_nested_terms_compare_unequal():
    duckdb = _duckdb()
    con = duckdb.connect()
    con.execute("CREATE TABLE terms (left_value VARCHAR NOT NULL, right_value VARCHAR NOT NULL)")
    left = Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI")))
    right = Compound("role", (Compound("person", (StringLit("Ada"),)), Atom("pi")))

    con.execute(
        "INSERT INTO terms VALUES (?, ?)",
        [term_to_duckdb_value(left), term_to_duckdb_value(right)],
    )

    assert con.execute(
        f"SELECT {term_eq_sql('left_value', 'right_value')} FROM terms"
    ).fetchone() == (False,)


def test_duckdb_acceptance_collisions_compare_unequal():
    duckdb = _duckdb()
    con = duckdb.connect()
    con.execute("CREATE TABLE terms (left_value VARCHAR NOT NULL, right_value VARCHAR NOT NULL)")
    pairs = [
        (Compound("f", (StringLit("A"),)), StringLit("f(A)")),
        (Compound("f", (Var("A"),)), Compound("f", (StringLit("A"),))),
    ]
    con.executemany(
        "INSERT INTO terms VALUES (?, ?)",
        [(term_to_duckdb_value(left), term_to_duckdb_value(right)) for left, right in pairs],
    )

    assert con.execute(
        f"SELECT bool_or({term_eq_sql('left_value', 'right_value')}) FROM terms"
    ).fetchone() == (False,)
