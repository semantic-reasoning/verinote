# SPDX-License-Identifier: MPL-2.0
import json
import sqlite3

import pytest

from verinote.engine.terms import (
    Atom,
    Compound,
    NumberLit,
    StringLit,
    TermParseError,
    Var,
)
from verinote.store import Store
from verinote.store.db import FACT_TERMS_MARKER_KEY
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStore,
    DuckDBFactTermStoreError,
    fact_term_token,
    fact_term_token_from_values,
    fact_terms_path,
)
from verinote.store.fact_input import structural_term
from verinote.text import nfc


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
_INVALID_CONFIDENCES = (True, None, "0.5", float("nan"), float("inf"), -0.1, 1.1)


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _legacy_string_value(value: str) -> str:
    return json.dumps({"t": "string", "v": value}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def test_store_init_schema_keeps_a_fresh_kb_without_a_fact_term_sidecar(tmp_path):
    s = _store(tmp_path)
    try:
        assert not fact_terms_path(tmp_path).exists()
    finally:
        s.close()


def test_store_init_schema_migrates_paired_nfd_tokens_before_write_transactions(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    bootstrap = _store(tmp_path)
    bootstrap.close()
    nfd_value = "Cafe\u0301"
    legacy_values = (
        _legacy_string_value(nfd_value),
        _legacy_string_value("rel"),
        _legacy_string_value(nfd_value),
    )
    legacy_token = fact_term_token_from_values(legacy_values)
    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        cur = sqlite_con.execute(
            """
            INSERT INTO facts(subject, relation, object, status, term_token)
            VALUES (?, ?, ?, 'confirmed', ?) RETURNING id
            """,
            (nfd_value, "rel", nfd_value, legacy_token),
        )
        fact_id = int(cur.fetchone()[0])
        sqlite_con.commit()
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)))
    try:
        con.execute(
            """
            CREATE TABLE fact_terms (
                fact_id BIGINT PRIMARY KEY,
                subject VARCHAR NOT NULL,
                rel VARCHAR NOT NULL,
                object VARCHAR NOT NULL,
                term_token VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO fact_terms VALUES (?, ?, ?, ?, ?)",
            [fact_id, *legacy_values, legacy_token],
        )
    finally:
        con.close()

    direct = DuckDBFactTermStore(fact_terms_path(tmp_path))
    try:
        with pytest.raises(DuckDBFactTermStoreError, match="not canonical"):
            direct.get_fact_terms(fact_id)
    finally:
        direct.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)), read_only=True)
    try:
        assert con.execute(
            "SELECT subject, rel, object, term_token FROM fact_terms WHERE fact_id = ?", [fact_id]
        ).fetchone() == (*legacy_values, legacy_token)
    finally:
        con.close()

    s = _store(tmp_path)
    nfc_value = nfc(nfd_value)
    expected_terms = (StringLit(nfc_value), StringLit("rel"), StringLit(nfc_value))
    expected_token = fact_term_token(*expected_terms)
    try:
        # `add_fact` opens a SQLite write transaction, then accesses fact_terms.
        # It is safe because init_schema already coordinated the legacy rewrite.
        s.add_fact("new", "rel", "value", status="candidate")
        assert s.engine_fact_terms() == [
            {
                "id": fact_id,
                "subject": expected_terms[0],
                "relation": expected_terms[1],
                "object": expected_terms[2],
            }
        ]
        assert s.get_fact(fact_id)["term_token"] == expected_token
        record = s.fact_terms.get_fact_term_record(fact_id)
        assert record is not None
        assert record.term_token == record.content_token == expected_token
    finally:
        s.close()

    reopened = _store(tmp_path)
    try:
        assert reopened.fact_terms.get_fact_term_record(fact_id) == record
    finally:
        reopened.close()


def test_store_backfills_tokens_for_a_canonical_four_column_sidecar(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    bootstrap = _store(tmp_path)
    bootstrap.close()
    legacy_values = (
        _legacy_string_value("subject"),
        _legacy_string_value("rel"),
        _legacy_string_value("object"),
    )
    token = fact_term_token_from_values(legacy_values)
    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        cur = sqlite_con.execute(
            """
            INSERT INTO facts(subject, relation, object, status, term_token)
            VALUES ('subject', 'rel', 'object', 'confirmed', ?) RETURNING id
            """,
            (token,),
        )
        fact_id = int(cur.fetchone()[0])
        sqlite_con.commit()
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)))
    try:
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
        con.execute("INSERT INTO fact_terms VALUES (?, ?, ?, ?)", [fact_id, *legacy_values])
    finally:
        con.close()

    s = _store(tmp_path)
    try:
        assert s.engine_fact_terms() == [
            {
                "id": fact_id,
                "subject": StringLit("subject"),
                "relation": StringLit("rel"),
                "object": StringLit("object"),
            }
        ]
        assert s.get_fact(fact_id)["term_token"] == token
        record = s.fact_terms.get_fact_term_record(fact_id)
        assert record is not None
        assert record.term_token == record.content_token == token
    finally:
        s.close()


def test_stale_four_column_nfc_migration_fails_during_store_initialization(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    bootstrap = _store(tmp_path)
    bootstrap.close()
    nfd_value = "Cafe\u0301"
    legacy_values = (
        _legacy_string_value(nfd_value),
        _legacy_string_value("rel"),
        _legacy_string_value(nfd_value),
    )
    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        cur = sqlite_con.execute(
            """
            INSERT INTO facts(subject, relation, object, status, term_token)
            VALUES (?, ?, ?, 'confirmed', ?) RETURNING id
            """,
            (nfd_value, "rel", nfd_value, "stale-token"),
        )
        fact_id = int(cur.fetchone()[0])
        sqlite_con.commit()
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)))
    try:
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
        con.execute("INSERT INTO fact_terms VALUES (?, ?, ?, ?)", [fact_id, *legacy_values])
    finally:
        con.close()

    s = Store(tmp_path / "kb.sqlite")
    try:
        with pytest.raises(DuckDBFactTermStoreError, match="stale DuckDB fact terms"):
            s.init_schema()
    finally:
        s.close()

    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        assert sqlite_con.execute(
            "SELECT term_token FROM facts WHERE id = ?", (fact_id,)
        ).fetchone() == ("stale-token",)
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)), read_only=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info('fact_terms')").fetchall()}
        assert columns == {"fact_id", "subject", "rel", "object"}
        assert con.execute(
            "SELECT subject, rel, object FROM fact_terms WHERE fact_id = ?", [fact_id]
        ).fetchone() == legacy_values
    finally:
        con.close()


def test_sqlite_commit_failure_compensates_a_four_column_nfc_migration(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    bootstrap = _store(tmp_path)
    bootstrap.close()
    nfd_value = "Cafe\u0301"
    legacy_values = (
        _legacy_string_value(nfd_value),
        _legacy_string_value("rel"),
        _legacy_string_value(nfd_value),
    )
    legacy_token = fact_term_token_from_values(legacy_values)
    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        cur = sqlite_con.execute(
            """
            INSERT INTO facts(subject, relation, object, status, term_token)
            VALUES (?, ?, ?, 'confirmed', ?) RETURNING id
            """,
            (nfd_value, "rel", nfd_value, legacy_token),
        )
        fact_id = int(cur.fetchone()[0])
        sqlite_con.commit()
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)))
    try:
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
        con.execute("INSERT INTO fact_terms VALUES (?, ?, ?, ?)", [fact_id, *legacy_values])
    finally:
        con.close()

    s = Store(tmp_path / "kb.sqlite")

    def fail_commit() -> None:
        raise sqlite3.OperationalError("forced SQLite commit failure")

    monkeypatch.setattr(s, "_commit_nfc_sqlite_migration", fail_commit)
    try:
        with pytest.raises(sqlite3.OperationalError, match="forced SQLite commit failure"):
            s.init_schema()
    finally:
        s.close()

    sqlite_con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        assert sqlite_con.execute(
            "SELECT term_token FROM facts WHERE id = ?", (fact_id,)
        ).fetchone() == (legacy_token,)
    finally:
        sqlite_con.close()
    con = duckdb.connect(str(fact_terms_path(tmp_path)), read_only=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info('fact_terms')").fetchall()}
        assert columns == {"fact_id", "subject", "rel", "object"}
        assert con.execute(
            "SELECT subject, rel, object FROM fact_terms WHERE fact_id = ?", [fact_id]
        ).fetchone() == legacy_values
    finally:
        con.close()


def test_add_fact_writes_sqlite_metadata_and_stringlit_terms(tmp_path):
    s = _store(tmp_path)

    fid = s.add_fact('person("Ada")', "is_a", "person", status="candidate", confidence=0.7)

    row = s.get_fact(fid)
    assert (row["subject"], row["relation"], row["object"], row["status"]) == (
        'person("Ada")',
        "is_a",
        "person",
        "candidate",
    )
    assert s.get_fact_terms(fid) == (
        StringLit('person("Ada")'),
        StringLit("is_a"),
        StringLit("person"),
    )


def test_add_fact_accepts_structural_terms_without_parsing_strings(tmp_path):
    s = _store(tmp_path)

    fid = s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("has_role"),
        Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
    )

    row = s.get_fact(fid)
    assert (row["subject"], row["relation"], row["object"]) == (
        'person("Ada")',
        "has_role",
        'role(person("Ada"), "PI")',
    )
    assert s.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("has_role"),
        Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))),
    )


def test_add_fact_accepts_a_shared_acyclic_compound_dag(tmp_path):
    s = _store(tmp_path)
    shared = Compound("child", (StringLit("synthetic"),))
    parent = Compound("parent", (shared, shared))

    assert parent.args[0] is parent.args[1]
    fid = s.add_fact(parent, "rel", "object")

    assert s.get_fact_terms(fid) == (
        Compound(
            "parent",
            (
                Compound("child", (StringLit("synthetic"),)),
                Compound("child", (StringLit("synthetic"),)),
            ),
        ),
        StringLit("rel"),
        StringLit("object"),
    )


def test_structural_term_is_an_explicit_input_boundary(tmp_path):
    s = _store(tmp_path)

    plain_id = s.add_fact('person("Ada")', "is_a", "person")
    term_id = s.add_fact(
        structural_term('person("Ada")'),
        structural_term("is_a"),
        structural_term("1815"),
    )

    assert s.get_fact(plain_id)["subject"] == s.get_fact(term_id)["subject"]
    assert s.get_fact_terms(plain_id) == (
        StringLit('person("Ada")'),
        StringLit("is_a"),
        StringLit("person"),
    )
    assert s.get_fact_terms(term_id) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("is_a"),
        NumberLit(1815),
    )


def test_structural_term_rejects_invalid_or_nonground_terms(tmp_path):
    s = _store(tmp_path)

    with pytest.raises(TermParseError):
        structural_term('person("Ada"')
    with pytest.raises(TermParseError, match="ground"):
        structural_term("person(X)")

    assert s.facts() == []


def test_store_rejects_direct_nonground_term_inputs_without_writing(tmp_path):
    s = _store(tmp_path)

    with pytest.raises(ValueError, match="ground"):
        s.add_fact(Var("S"), "r", "x")
    with pytest.raises(ValueError, match="ground"):
        s.add_fact(Compound("person", (Var("Name"),)), "r", "x")

    assert s.facts() == []
    assert s.fact_terms.get_many_fact_terms([1, 2]) == {}


@pytest.mark.parametrize("value", _INVALID_FACT_SLOTS)
def test_add_fact_rejects_invalid_slots_before_any_storage_write(tmp_path, value):
    s = _store(tmp_path)

    with pytest.raises(ValueError):
        s.add_fact(value, "rel", "object")

    assert s.facts() == []
    assert not fact_terms_path(tmp_path).exists()
    assert s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0] == 0


@pytest.mark.parametrize("confidence", _INVALID_CONFIDENCES)
def test_add_fact_rejects_invalid_confidence_before_any_storage_write(tmp_path, confidence):
    s = _store(tmp_path)

    with pytest.raises(ValueError, match="confidence"):
        s.add_fact("subject", "rel", "object", confidence=confidence)

    assert s.facts() == []
    assert not fact_terms_path(tmp_path).exists()
    assert s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0] == 0


def test_fact_terms_sidecar_persists_across_store_reopen(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact(Compound("date", (NumberLit(2020), NumberLit(1), NumberLit(1))), "r", "x")
    s.close()

    reopened = Store(tmp_path / "kb.sqlite")
    try:
        assert fact_terms_path(tmp_path).is_file()
        assert reopened.get_fact_terms(fid) == (
            Compound("date", (NumberLit(2020), NumberLit(1), NumberLit(1))),
            StringLit("r"),
            StringLit("x"),
        )
    finally:
        reopened.close()


def test_amend_fact_updates_sqlite_duckdb_terms_and_audit(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")

    decision = s.amend_fact(
        fid,
        subject=Compound("person", (StringLit("Ada"),)),
        relation=Atom("born_year"),
        obj=NumberLit(1815),
        note="fixed",
    )
    after = decision.fact

    assert decision.changed is True
    assert (after["subject"], after["relation"], after["object"], after["note"]) == (
        'person("Ada")',
        "born_year",
        "1815",
        "fixed",
    )
    assert s.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("born_year"),
        NumberLit(1815),
    )
    assert [e["action"] for e in s.fact_log(fid)] == ["amended"]


def test_amend_fact_keeps_term_syntax_strings_as_stringlit(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact(Compound("person", (StringLit("Ada"),)), Atom("born_in"), "London")

    s.amend_fact(
        fid,
        subject='person("Ada")',
        relation="born_in",
        obj='city("London")',
    )

    assert s.get_fact_terms(fid) == (
        StringLit('person("Ada")'),
        StringLit("born_in"),
        StringLit('city("London")'),
    )


def test_replayed_structural_amend_writes_no_audit_event(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("born_year"),
        NumberLit(1815),
        note="verified",
    )
    before = dict(s.get_fact(fid))
    before_terms = s.get_fact_terms(fid)

    decision = s.amend_fact(
        fid,
        subject=Compound("person", (StringLit("Ada"),)),
        relation=Atom("born_year"),
        obj=NumberLit(1815),
        note="verified",
    )

    assert decision.changed is False
    assert dict(decision.fact) == before
    assert s.get_fact_terms(fid) == before_terms
    assert s.fact_log(fid) == []


def test_backfill_fact_terms_migrates_legacy_sqlite_text_as_stringlit(tmp_path):
    s = _store(tmp_path)
    cur = s._conn.execute(
        "INSERT INTO facts(subject, relation, object, status) VALUES(?,?,?,?) RETURNING id",
        ('person("Ada")', "is_a", "person", "confirmed"),
    )
    fid = int(cur.fetchone()[0])

    assert s.backfill_fact_terms() == 1
    assert s.get_fact_terms(fid) == (
        StringLit('person("Ada")'),
        StringLit("is_a"),
        StringLit("person"),
    )
    assert s._get_meta(FACT_TERMS_MARKER_KEY) is not None
    assert s.backfill_fact_terms() == 0


def test_store_migrates_existing_facts_table_to_add_term_token(tmp_path):
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.execute(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY,
            subject TEXT NOT NULL,
            relation TEXT NOT NULL,
            object TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            confidence REAL NOT NULL DEFAULT 0.0,
            source_id INTEGER,
            run_id INTEGER,
            job_id INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.close()

    reopened = _store(tmp_path)
    try:
        columns = {
            row["name"] for row in reopened._conn.execute("PRAGMA table_info(facts)")
        }
        assert "term_token" in columns
    finally:
        reopened.close()


def test_engine_fact_terms_rejects_missing_modern_sidecar_terms(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("is_a"),
        StringLit("person"),
        status="confirmed",
    )
    s.fact_terms.delete_fact_terms(fid)

    with pytest.raises(DuckDBFactTermStoreError, match="Refusing to rebuild"):
        s.engine_fact_terms()

    assert s.get_fact_terms(fid) is None


def test_store_init_schema_refuses_a_corrupt_existing_sidecar(tmp_path):
    # Existing sidecars are opened at initialization so corruption cannot be
    # deferred until a later SQLite write transaction first accesses fact_terms.
    seed = _store(tmp_path)
    fid = seed.add_fact(
        Compound("person", (StringLit("Ada"),)),
        Atom("born_in"),
        StringLit("London"),
        status="needs_review",
    )
    token = seed.get_fact(fid)["term_token"]
    seed.close()
    fact_terms_path(tmp_path).write_bytes(b"not a duckdb database file" * 500)

    store = Store(tmp_path / "kb.sqlite")
    try:
        with pytest.raises(DuckDBFactTermStoreError, match="failed to open DuckDB fact-term store"):
            store.init_schema()
        # SQLite was not modified before the corrupt sidecar error surfaced.
        assert store.get_fact(fid)["term_token"] == token
    finally:
        store.close()


def test_engine_fact_terms_rejects_stale_modern_sidecar_terms(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("Ada", "born_in", "Paris", status="confirmed")

    # Simulate an interrupted amend after SQLite committed its new display/token
    # but before facts.duckdb received the matching logical terms.
    s._conn.execute(
        "UPDATE facts SET object = ?, term_token = ? WHERE id = ?",
        ("London", "0" * 64, fid),
    )

    with pytest.raises(DuckDBFactTermStoreError, match="stale DuckDB fact terms"):
        s.engine_fact_terms()


def test_engine_fact_terms_rejects_missing_modern_sidecar_token(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("Ada", "born_in", "Paris", status="confirmed")
    s.fact_terms._execute("UPDATE fact_terms SET term_token = NULL WHERE fact_id = ?", [fid])

    with pytest.raises(DuckDBFactTermStoreError, match="stale DuckDB fact terms"):
        s.engine_fact_terms()


def test_engine_fact_terms_marks_complete_pre_marker_sidecar(tmp_path):
    s = _store(tmp_path)
    cur = s._conn.execute(
        "INSERT INTO facts(subject, relation, object, status) VALUES(?,?,?,?) RETURNING id",
        ('person("Ada")', "is_a", "person", "confirmed"),
    )
    fid = int(cur.fetchone()[0])
    s.fact_terms.put_fact_terms(
        fid,
        Compound("person", (StringLit("Ada"),)),
        Atom("is_a"),
        StringLit("person"),
    )
    assert s._get_meta(FACT_TERMS_MARKER_KEY) is None

    assert s.engine_fact_terms() == [
        {
            "id": fid,
            "subject": Compound("person", (StringLit("Ada"),)),
            "relation": Atom("is_a"),
            "object": StringLit("person"),
        }
    ]
    assert s._get_meta(FACT_TERMS_MARKER_KEY) is not None

    s.fact_terms.delete_fact_terms(fid)
    with pytest.raises(DuckDBFactTermStoreError, match="Refusing to rebuild"):
        s.engine_fact_terms()


def test_backfill_fact_terms_rejects_missing_terms_after_sidecar_marker(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("Ada", "born_in", "London", status="confirmed")
    s.fact_terms.delete_fact_terms(fid)

    with pytest.raises(DuckDBFactTermStoreError, match="missing DuckDB fact terms"):
        s.backfill_fact_terms()

    assert s.get_fact_terms(fid) is None


def test_backfill_fact_terms_does_not_overwrite_structural_terms(tmp_path):
    s = _store(tmp_path)
    cur = s._conn.execute(
        "INSERT INTO facts(subject, relation, object, status) VALUES(?,?,?,?) RETURNING id",
        ('person("Ada")', "is_a", "person", "confirmed"),
    )
    fid = int(cur.fetchone()[0])
    s.fact_terms.put_fact_terms(
        fid,
        Compound("person", (StringLit("Ada"),)),
        Atom("is_a"),
        StringLit("person"),
    )

    assert s.backfill_fact_terms() == 0
    assert s.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("is_a"),
        StringLit("person"),
    )


def test_status_changes_do_not_mutate_duckdb_terms(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact(Compound("person", (StringLit("Ada"),)), "r", "x", status="needs_review")
    before = s.get_fact_terms(fid)

    s.toggle_review(fid)
    s.reject_fact(fid)

    assert s.get_fact_terms(fid) == before


def test_add_fact_duckdb_failure_rolls_back_sqlite_insert(tmp_path, monkeypatch):
    s = _store(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(s.fact_terms, "put_fact_terms", fail)

    with pytest.raises(RuntimeError, match="sidecar down"):
        s.add_fact("A", "r", "B", status="confirmed")

    assert s.facts() == []


def test_amend_fact_duckdb_failure_rolls_back_sqlite_leaving_no_divergence(
    tmp_path, monkeypatch
):
    # The DuckDB write runs inside the amend's SQLite transaction, so a write-time
    # sidecar failure rolls the SQLite update back too: the stores move together
    # instead of leaving SQLite ahead of a stale DuckDB (the divergence that used
    # to surface later as a confusing "stale DuckDB fact terms" engine error).
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="confirmed", note="orig")
    before_terms = s.get_fact_terms(fid)

    def fail(*args, **kwargs):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(s.fact_terms, "put_fact_terms", fail)

    with pytest.raises(RuntimeError, match="sidecar down"):
        s.amend_fact(fid, subject="A2", relation="r2", obj="B2", note="changed")

    row = s.get_fact(fid)
    assert (row["subject"], row["relation"], row["object"], row["note"]) == (
        "A",
        "r",
        "B",
        "orig",
    )
    assert s.fact_log(fid) == []
    assert s.get_fact_terms(fid) == before_terms
    # The stores still agree, so the engine reads cleanly -- no stale divergence.
    assert len(s.engine_fact_terms()) == 1


def test_amend_retry_self_heals_when_duckdb_is_ahead_of_stale_sqlite(tmp_path):
    # Simulate the residual divergence the reordered write cannot fully close: a
    # prior amend wrote DuckDB but its SQLite COMMIT failed, leaving DuckDB ahead
    # of a stale SQLite row. A retry of the same edit must PROCEED and correct
    # SQLite -- not early-return "unchanged" on the strength of DuckDB alone,
    # which would mask the stale row forever. The no-op guard also compares
    # SQLite's own term_token, so the retry self-heals.
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review")

    new_subject = structural_term('person("Ada")')
    new_relation = structural_term("born_in")
    new_object = "London"
    # DuckDB ahead of the request; SQLite still holds the stale "A"/"r"/"B" row.
    s.fact_terms.put_fact_terms(
        fid,
        new_subject,
        new_relation,
        new_object,
        term_token=fact_term_token(new_subject, new_relation, new_object),
    )

    decision = s.amend_fact(
        fid, subject=new_subject, relation=new_relation, obj=new_object, note=""
    )

    assert decision.changed is True
    row = s.get_fact(fid)
    assert (row["subject"], row["relation"], row["object"]) == (
        'person("Ada")',
        "born_in",
        "London",
    )
    assert s.get_fact_terms(fid) == (
        Compound("person", (StringLit("Ada"),)),
        Atom("born_in"),
        StringLit("London"),
    )


def test_amend_fact_rejects_direct_nonground_terms_and_restores_state(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review", note="orig")
    before = dict(s.get_fact(fid))
    before_terms = s.get_fact_terms(fid)

    with pytest.raises(ValueError, match="ground"):
        s.amend_fact(
            fid,
            subject=Compound("person", (Var("Name"),)),
            relation="r",
            obj="B2",
            note="bad",
        )

    assert dict(s.get_fact(fid)) == before
    assert s.get_fact_terms(fid) == before_terms
    assert s.fact_log(fid) == []


@pytest.mark.parametrize("value", _INVALID_FACT_SLOTS)
def test_amend_fact_rejects_invalid_slots_without_partial_state(tmp_path, value):
    s = _store(tmp_path)
    fid = s.add_fact("A", "rel", "B", status="needs_review", note="original")
    before = dict(s.get_fact(fid))
    before_terms = s.get_fact_terms(fid)

    with pytest.raises(ValueError):
        s.amend_fact(fid, subject="A2", relation="rel", obj=value, note="changed")

    assert dict(s.get_fact(fid)) == before
    assert s.get_fact_terms(fid) == before_terms
    assert s.fact_log(fid) == []


@pytest.mark.parametrize("confidence", _INVALID_CONFIDENCES)
def test_reconcile_duplicate_rejects_invalid_confidence_without_suppression_event(
    tmp_path, confidence
):
    s = _store(tmp_path)
    source_id = s.add_source("synthetic-source.txt")
    fid = s.add_fact("A", "rel", "B", source_id=source_id)
    s.reject_fact(fid)
    before_events = s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0]

    with pytest.raises(ValueError, match="confidence"):
        s.reconcile_fact("A", "rel", "B", source_id=source_id, confidence=confidence)

    assert len(s.facts()) == 1
    assert s.get_fact_terms(fid) == (StringLit("A"), StringLit("rel"), StringLit("B"))
    assert s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0] == before_events


def test_reconcile_duplicate_rejects_invalid_slot_without_suppression_event(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("synthetic-source.txt")
    fid = s.add_fact("A", "rel", "36", source_id=source_id)
    s.reject_fact(fid)
    before_events = s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0]

    with pytest.raises(ValueError):
        s.reconcile_fact("A", "rel", 36, source_id=source_id)

    assert len(s.facts()) == 1
    assert s.get_fact_terms(fid) == (StringLit("A"), StringLit("rel"), StringLit("36"))
    assert s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0] == before_events


def test_reconcile_rejects_nested_malformed_stringlit_without_partial_state(tmp_path):
    s = _store(tmp_path)
    source_id = s.add_source("synthetic-source.txt")
    fid = s.add_fact("A", "rel", "B", source_id=source_id)
    before_events = s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0]
    malformed = Compound("person", (_malformed_stringlit(),))

    with pytest.raises(ValueError, match="StringLit"):
        s.reconcile_fact("A", "rel", malformed, source_id=source_id)

    assert len(s.facts()) == 1
    assert s.get_fact_terms(fid) == (StringLit("A"), StringLit("rel"), StringLit("B"))
    assert s._conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0] == before_events


def test_amend_fact_audit_failure_rolls_back_sqlite_and_restores_terms(
    tmp_path, monkeypatch
):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review", note="orig")
    before_terms = s.get_fact_terms(fid)

    def fail_log(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(s, "_log", fail_log)

    with pytest.raises(RuntimeError, match="audit down"):
        s.amend_fact(
            fid,
            subject=Compound("person", (StringLit("Ada"),)),
            relation=Atom("born_year"),
            obj=NumberLit(1815),
            note="changed",
        )

    row = s.get_fact(fid)
    assert (row["subject"], row["relation"], row["object"], row["note"]) == (
        "A",
        "r",
        "B",
        "orig",
    )
    assert s.fact_log(fid) == []
    assert s.get_fact_terms(fid) == before_terms


def test_store_close_closes_fact_term_store(tmp_path):
    s = _store(tmp_path)
    _ = s.fact_terms

    assert s._fact_terms is not None
    s.close()

    assert s._fact_terms is None
