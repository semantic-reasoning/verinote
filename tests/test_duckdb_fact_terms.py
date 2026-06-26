# SPDX-License-Identifier: MPL-2.0
import builtins

import pytest

from verinote.engine.duckdb_terms import term_to_duckdb_value
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStore,
    DuckDBFactTermStoreError,
    fact_terms_path,
)


def _duckdb():
    return pytest.importorskip("duckdb")


def test_fact_terms_path_uses_kb_root():
    assert fact_terms_path("/tmp/kb").as_posix() == "/tmp/kb/facts.duckdb"


def test_store_round_trips_plain_strings_as_string_terms():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "person(\"Ada\")", "is_a", "person")

        assert store.get_fact_terms(1) == (
            StringLit('person("Ada")'),
            StringLit("is_a"),
            StringLit("person"),
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("fact_id", "triple"),
    [
        (1, (Atom("ada"), Atom("born_in"), StringLit("London"))),
        (2, (StringLit("Ada"), Atom("born_year"), NumberLit(1815))),
        (
            3,
            (
                Compound("person", (StringLit("Ada"),)),
                Atom("has_role"),
                Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
            ),
        ),
        (
            4,
            (
                Compound("grant", (StringLit("NSF"), StringLit("123"))),
                Atom("starts_on"),
                Compound("date", (NumberLit(2020), NumberLit(1), NumberLit(1))),
            ),
        ),
    ],
)
def test_store_round_trips_structural_terms(fact_id, triple):
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(fact_id, *triple)

        assert store.get_fact_terms(fact_id) == triple
    finally:
        store.close()


def test_store_preserves_term_type_distinctions():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, StringLit("ada"), Atom("rel"), StringLit("x"))
        store.put_fact_terms(2, Atom("ada"), Atom("rel"), StringLit("x"))

        assert store.get_fact_terms(1)[0] == StringLit("ada")
        assert store.get_fact_terms(2)[0] == Atom("ada")
        assert store.get_fact_terms(1) != store.get_fact_terms(2)
    finally:
        store.close()


def test_store_upsert_latest_terms_win():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "A", "r", "B")
        store.put_fact_terms(1, Compound("person", (StringLit("Ada"),)), Atom("r"), NumberLit(1))

        assert store.get_fact_terms(1) == (
            Compound("person", (StringLit("Ada"),)),
            Atom("r"),
            NumberLit(1),
        )
    finally:
        store.close()


def test_store_delete_existing_and_missing_terms():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "A", "r", "B")
        store.delete_fact_terms(1)
        store.delete_fact_terms(1)

        assert store.get_fact_terms(1) is None
    finally:
        store.close()


def test_store_get_many_handles_empty_missing_and_duplicate_ids():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "A", "r", "B")
        store.put_fact_terms(2, Atom("ada"), Atom("rel"), StringLit("x"))

        assert store.get_many_fact_terms([]) == {}
        result = store.get_many_fact_terms([2, 99, 1, 1])
        assert list(result) == [1, 2]
        assert result == {
            1: (StringLit("A"), StringLit("r"), StringLit("B")),
            2: (Atom("ada"), Atom("rel"), StringLit("x")),
        }
    finally:
        store.close()


def test_store_reopens_durable_file(tmp_path):
    _duckdb()
    path = fact_terms_path(tmp_path)
    store = DuckDBFactTermStore(path)
    store.put_fact_terms(1, Compound("person", (StringLit("Ada"),)), Atom("rel"), StringLit("x"))
    store.close()

    reopened = DuckDBFactTermStore(path)
    try:
        assert reopened.get_fact_terms(1) == (
            Compound("person", (StringLit("Ada"),)),
            Atom("rel"),
            StringLit("x"),
        )
    finally:
        reopened.close()


def test_store_schema_initialization_is_idempotent(tmp_path):
    _duckdb()
    path = fact_terms_path(tmp_path)
    first = DuckDBFactTermStore(path)
    first.close()
    second = DuckDBFactTermStore(path)
    try:
        second.init_schema()
        second.put_fact_terms(1, "A", "r", "B")
        assert second.get_fact_terms(1) == (StringLit("A"), StringLit("r"), StringLit("B"))
    finally:
        second.close()


@pytest.mark.parametrize("fact_id", [0, -1, True, "1"])
def test_store_rejects_invalid_fact_ids(fact_id):
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        with pytest.raises(DuckDBFactTermStoreError, match="fact_id"):
            store.put_fact_terms(fact_id, "A", "r", "B")
    finally:
        store.close()


def test_store_reports_closed_connection():
    _duckdb()
    store = DuckDBFactTermStore(None)
    store.close()
    store.close()

    with pytest.raises(DuckDBFactTermStoreError, match="closed"):
        store.get_fact_terms(1)


def test_store_reports_malformed_payload_with_fact_and_column_context():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store._execute(
            "INSERT INTO fact_terms VALUES (?, ?, ?, ?)",
            [
                1,
                '{"t":"atom","v":"Bad"}',
                term_to_duckdb_value(Atom("rel")),
                term_to_duckdb_value(StringLit("x")),
            ],
        )

        with pytest.raises(DuckDBFactTermStoreError, match="fact_id=1 column=subject"):
            store.get_fact_terms(1)
    finally:
        store.close()


def test_store_reports_missing_duckdb(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(DuckDBFactTermStoreError, match="DuckDB is not installed"):
        DuckDBFactTermStore(None)
