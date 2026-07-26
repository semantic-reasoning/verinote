# SPDX-License-Identifier: MPL-2.0
import builtins
import json
import sys

import pytest

from verinote.engine.duckdb_terms import DuckDBTermError, duckdb_value_to_term, term_to_duckdb_value
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit, Var
from verinote.store.duckdb_fact_terms import (
    FACT_TERMS_FILENAME,
    DuckDBFactTermStore,
    DuckDBFactTermStoreError,
    fact_term_token,
    fact_term_token_from_values,
    fact_terms_path,
)
from verinote.text import nfc


def _duckdb():
    return pytest.importorskip("duckdb")


def _malformed_stringlit() -> StringLit:
    term = StringLit("synthetic")
    object.__setattr__(term, "value", 7)
    return term


def _malformed_atom() -> Atom:
    term = Atom("synthetic")
    object.__setattr__(term, "name", "Synthetic")
    return term


def _malformed_numberlit() -> NumberLit:
    term = NumberLit(1)
    object.__setattr__(term, "value", True)
    return term


def _malformed_compound_functor() -> Compound:
    term = Compound("synthetic", (StringLit("value"),))
    object.__setattr__(term, "functor", "Synthetic")
    return term


def _malformed_compound_args() -> Compound:
    term = Compound("synthetic", (StringLit("value"),))
    object.__setattr__(term, "args", [])
    return term


def _cyclic_compound() -> Compound:
    term = Compound("synthetic", ())
    object.__setattr__(term, "args", (term,))
    return term


def _legacy_term_value(payload: dict[str, object]) -> str:
    """Build a canonical payload from before StringLit NFC encoding."""
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _create_raw_fact_terms(
    path, rows: list[tuple[object, ...]], *, has_term_token: bool = True
) -> None:
    duckdb = _duckdb()
    con = duckdb.connect(str(path))
    try:
        token_column = ", term_token VARCHAR" if has_term_token else ""
        placeholders = "?, ?, ?, ?, ?" if has_term_token else "?, ?, ?, ?"
        con.execute(
            f"""
            CREATE TABLE fact_terms (
                fact_id BIGINT PRIMARY KEY,
                subject VARCHAR NOT NULL,
                rel VARCHAR NOT NULL,
                object VARCHAR NOT NULL{token_column}
            )
            """
        )
        con.executemany(f"INSERT INTO fact_terms VALUES ({placeholders})", rows)
    finally:
        con.close()


def _raw_fact_term_row(path, fact_id: int) -> tuple[object, ...]:
    duckdb = _duckdb()
    con = duckdb.connect(str(path), read_only=True)
    try:
        row = con.execute(
            "SELECT subject, rel, object, term_token FROM fact_terms WHERE fact_id = ?",
            [fact_id],
        ).fetchone()
        assert row is not None
        return row
    finally:
        con.close()


_INVALID_FACT_SLOTS = (
    7,
    1.5,
    True,
    None,
    [],
    (),
    {},
    {"value"},
    object(),
    _malformed_stringlit(),
    Compound("person", (_malformed_stringlit(),)),
    _malformed_atom(),
    Compound("person", (_malformed_atom(),)),
    _malformed_numberlit(),
    Compound("person", (_malformed_numberlit(),)),
    _malformed_compound_functor(),
    Compound("person", (_malformed_compound_functor(),)),
    _malformed_compound_args(),
    Compound("person", (_malformed_compound_args(),)),
    _cyclic_compound(),
    Compound("person", (_cyclic_compound(),)),
    Var("Slot"),
    Compound("person", (Var("Name"),)),
)


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


def test_store_records_content_token_without_changing_tuple_api():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        triple = (Compound("person", (StringLit("Ada"),)), Atom("r"), NumberLit(1))
        store.put_fact_terms(1, *triple)

        assert store.get_fact_terms(1) == triple
        record = store.get_fact_term_record(1)
        assert record is not None
        assert record.terms == triple
        assert record.term_token == fact_term_token(*triple)
        assert record.content_token == fact_term_token(*triple)
    finally:
        store.close()


def test_store_rejects_a_supplied_token_that_does_not_match_payload():
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        with pytest.raises(DuckDBFactTermStoreError, match="token does not match"):
            store.put_fact_terms(1, "A", "r", "B", term_token="0" * 64)
        assert store.get_fact_terms(1) is None
    finally:
        store.close()


@pytest.mark.parametrize("value", _INVALID_FACT_SLOTS)
def test_sidecar_and_token_reject_invalid_slots_without_partial_state(value):
    _duckdb()
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "A", "rel", "B")
        before = store.get_fact_term_record(1)

        with pytest.raises(DuckDBFactTermStoreError):
            store.put_fact_terms(1, "A2", "rel", value)
        with pytest.raises(DuckDBFactTermStoreError):
            fact_term_token("A2", "rel", value)

        assert store.get_fact_term_record(1) == before
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


def test_direct_store_leaves_existing_four_column_table_unchanged(tmp_path):
    duckdb = _duckdb()
    path = fact_terms_path(tmp_path)
    con = duckdb.connect(str(path))
    con.execute(
        """
        CREATE TABLE fact_terms (
            fact_id BIGINT PRIMARY KEY,
            subject VARCHAR NOT NULL,
            rel VARCHAR NOT NULL,
            object VARCHAR NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO fact_terms VALUES (?, ?, ?, ?)",
        [
            1,
            term_to_duckdb_value(StringLit("A")),
            term_to_duckdb_value(StringLit("r")),
            term_to_duckdb_value(StringLit("B")),
        ],
    )
    con.close()

    store = DuckDBFactTermStore(path)
    try:
        plan = store.plan_nfc_migration()
        assert plan.needs_term_token is True
        assert len(plan.rewrites) == 1
        assert plan.rewrites[0].new_values == (
            term_to_duckdb_value(StringLit("A")),
            term_to_duckdb_value(StringLit("r")),
            term_to_duckdb_value(StringLit("B")),
        )
    finally:
        store.close()

    con = duckdb.connect(str(path), read_only=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info('fact_terms')").fetchall()}
        assert columns == {"fact_id", "subject", "rel", "object"}
    finally:
        con.close()


def test_direct_store_rejects_legacy_nfd_payloads_without_mutating_them(tmp_path):
    _duckdb()
    path = fact_terms_path(tmp_path)
    nfd_value = "Cafe\u0301"
    legacy_values = (
        _legacy_term_value(
            {
                "t": "compound",
                "f": "person",
                "a": [{"t": "string", "v": nfd_value}],
            }
        ),
        _legacy_term_value({"t": "string", "v": "has_label"}),
        _legacy_term_value(
            {
                "t": "compound",
                "f": "record",
                "a": [
                    {
                        "t": "compound",
                        "f": "label",
                        "a": [{"t": "string", "v": nfd_value}],
                    }
                ],
            }
        ),
    )
    _create_raw_fact_terms(
        path,
        [(1, *legacy_values, fact_term_token_from_values(legacy_values))],
    )

    # The production decoder stays strict; only initialization can read this
    # former, structurally canonical encoding.
    with pytest.raises(DuckDBTermError, match="not canonical"):
        duckdb_value_to_term(legacy_values[0])

    store = DuckDBFactTermStore(path)
    try:
        plan = store.plan_nfc_migration()
        assert len(plan.rewrites) == 1
        with pytest.raises(DuckDBFactTermStoreError, match="not canonical"):
            store.get_fact_terms(1)
    finally:
        store.close()

    assert _raw_fact_term_row(path, 1) == (
        *legacy_values,
        fact_term_token_from_values(legacy_values),
    )


def test_new_direct_and_nested_nfd_terms_have_nfc_identical_storage_and_tokens():
    _duckdb()
    store = DuckDBFactTermStore(None)
    nfd_value = "Cafe\u0301"
    nfc_value = nfc(nfd_value)
    direct_nfd = (StringLit(nfd_value), StringLit("rel"), StringLit(nfd_value))
    direct_nfc = (StringLit(nfc_value), StringLit("rel"), StringLit(nfc_value))
    nested_nfd = (
        Compound("outer", (StringLit(nfd_value), Compound("inner", (StringLit(nfd_value),)))),
        Atom("rel"),
        Compound("target", (StringLit(nfd_value),)),
    )
    nested_nfc = (
        Compound("outer", (StringLit(nfc_value), Compound("inner", (StringLit(nfc_value),)))),
        Atom("rel"),
        Compound("target", (StringLit(nfc_value),)),
    )
    try:
        store.put_fact_terms(1, *direct_nfd)
        store.put_fact_terms(2, *direct_nfc)
        store.put_fact_terms(3, *nested_nfd)
        store.put_fact_terms(4, *nested_nfc)

        with store._operation() as con:
            rows = {
                int(row[0]): row[1:]
                for row in con.execute(
                    "SELECT fact_id, subject, rel, object, term_token FROM fact_terms ORDER BY fact_id"
                ).fetchall()
            }
        assert rows[1] == rows[2]
        assert rows[3] == rows[4]
        assert rows[1][-1] == fact_term_token(*direct_nfc)
        assert rows[3][-1] == fact_term_token(*nested_nfc)
        assert store.get_fact_terms(1) == direct_nfc
        assert store.get_fact_terms(3) == nested_nfc
    finally:
        store.close()


def test_direct_nfc_migration_planning_is_idempotent_without_mutation(tmp_path):
    _duckdb()
    path = fact_terms_path(tmp_path)
    nfd_value = "Cafe\u0301"
    legacy_values = (
        _legacy_term_value({"t": "string", "v": nfd_value}),
        _legacy_term_value({"t": "string", "v": "rel"}),
        _legacy_term_value({"t": "string", "v": nfd_value}),
    )
    _create_raw_fact_terms(
        path,
        [(1, *legacy_values, fact_term_token_from_values(legacy_values))],
    )

    first = DuckDBFactTermStore(path)
    try:
        first_plan = first.plan_nfc_migration()
    finally:
        first.close()
    after_first_plan = _raw_fact_term_row(path, 1)

    second = DuckDBFactTermStore(path)
    try:
        second_plan = second.plan_nfc_migration()
    finally:
        second.close()

    assert second_plan == first_plan
    assert _raw_fact_term_row(path, 1) == after_first_plan


def test_nfc_fact_term_migration_rolls_back_when_a_legacy_row_is_malformed(tmp_path):
    _duckdb()
    path = fact_terms_path(tmp_path)
    nfd_value = "Cafe\u0301"
    valid_values = (
        _legacy_term_value({"t": "string", "v": nfd_value}),
        _legacy_term_value({"t": "string", "v": "rel"}),
        _legacy_term_value({"t": "string", "v": nfd_value}),
    )
    malformed_values = (
        "not json",
        _legacy_term_value({"t": "string", "v": "rel"}),
        _legacy_term_value({"t": "string", "v": "value"}),
    )
    _create_raw_fact_terms(
        path,
        [
            (1, *valid_values, fact_term_token_from_values(valid_values)),
            (2, *malformed_values, fact_term_token_from_values(malformed_values)),
        ],
    )

    store = DuckDBFactTermStore(path)
    try:
        with pytest.raises(
            DuckDBFactTermStoreError, match="fact_id=2 column=subject"
        ):
            store.plan_nfc_migration()
    finally:
        store.close()

    assert _raw_fact_term_row(path, 1) == (*valid_values, fact_term_token_from_values(valid_values))


def test_nfc_migration_keeps_a_malformed_four_column_table_unchanged(tmp_path):
    duckdb = _duckdb()
    path = fact_terms_path(tmp_path)
    nfd_value = "Cafe\u0301"
    valid_values = (
        _legacy_term_value({"t": "string", "v": nfd_value}),
        _legacy_term_value({"t": "string", "v": "rel"}),
        _legacy_term_value({"t": "string", "v": nfd_value}),
    )
    malformed_values = (
        "not json",
        _legacy_term_value({"t": "string", "v": "rel"}),
        _legacy_term_value({"t": "string", "v": "value"}),
    )
    _create_raw_fact_terms(
        path,
        [(1, *valid_values), (2, *malformed_values)],
        has_term_token=False,
    )

    store = DuckDBFactTermStore(path)
    try:
        with pytest.raises(
            DuckDBFactTermStoreError, match="fact_id=2 column=subject"
        ):
            store.plan_nfc_migration()
    finally:
        store.close()

    con = duckdb.connect(str(path), read_only=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info('fact_terms')").fetchall()}
        assert columns == {"fact_id", "subject", "rel", "object"}
        assert con.execute(
            "SELECT subject, rel, object FROM fact_terms WHERE fact_id = ?", [1]
        ).fetchone() == valid_values
    finally:
        con.close()


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
            "INSERT INTO fact_terms (fact_id, subject, rel, object) VALUES (?, ?, ?, ?)",
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


# DuckDB's native storage splits a database path on '?' and reads the tail as
# connection parameters, so a KB root containing one can never be opened: it used
# to surface as `Cannot open file ".../weird.wal?dir/facts.duckdb"` -- a filename
# nobody wrote -- after leaving a half-built facts.duckdb behind. The split
# happens inside DuckDB, below any string we control, so we refuse the path up
# front instead.
@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows forbids '?' in filenames")
def test_store_refuses_a_kb_root_with_a_question_mark(tmp_path):
    _duckdb()
    root = tmp_path / "weird?dir"
    root.mkdir()

    with pytest.raises(DuckDBFactTermStoreError) as excinfo:
        DuckDBFactTermStore.for_root(root)

    message = str(excinfo.value)
    assert "?" in message  # names the offending character
    assert str(root) in message  # and the path it is in
    assert "Move the KB" in message  # loud is not the same as a dead end: give a way out


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows forbids '?' in filenames")
def test_store_leaves_no_half_built_file_when_it_refuses_the_path(tmp_path):
    _duckdb()
    root = tmp_path / "weird?dir"
    root.mkdir()
    before = sorted(p.name for p in root.iterdir())

    with pytest.raises(DuckDBFactTermStoreError):
        DuckDBFactTermStore.for_root(root)

    after = sorted(p.name for p in root.iterdir())
    assert before == after == []
    assert not (root / FACT_TERMS_FILENAME).exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows forbids these in filenames")
@pytest.mark.parametrize("dirname", ["it's a kb", "semi;dir", "hash#dir"])
def test_store_still_opens_under_other_awkward_path_chars(tmp_path, dirname):
    # Only '?' is load-bearing for DuckDB's native storage. Do not over-reject.
    _duckdb()
    root = tmp_path / dirname
    root.mkdir()

    store = DuckDBFactTermStore.for_root(root)
    store.put_fact_terms(1, "A", "is_a", "B")
    assert store.get_fact_terms(1) is not None
    store.close()
