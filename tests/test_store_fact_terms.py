# SPDX-License-Identifier: MPL-2.0
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
from verinote.store.duckdb_fact_terms import fact_terms_path
from verinote.store.fact_input import structural_term


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


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

    after = s.amend_fact(
        fid,
        subject=Compound("person", (StringLit("Ada"),)),
        relation=Atom("born_year"),
        obj=NumberLit(1815),
        note="fixed",
    )

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
    assert s.backfill_fact_terms() == 0


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
    s.set_status(fid, "superseded")

    assert s.get_fact_terms(fid) == before


def test_add_fact_duckdb_failure_rolls_back_sqlite_insert(tmp_path, monkeypatch):
    s = _store(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(s.fact_terms, "put_fact_terms", fail)

    with pytest.raises(RuntimeError, match="sidecar down"):
        s.add_fact("A", "r", "B")

    assert s.facts() == []


def test_amend_fact_duckdb_failure_leaves_sqlite_and_audit_unchanged(tmp_path, monkeypatch):
    s = _store(tmp_path)
    fid = s.add_fact("A", "r", "B", status="needs_review", note="orig")
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
