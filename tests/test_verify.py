# SPDX-License-Identifier: MPL-2.0
import builtins
from pathlib import Path

import verinote.engine.duckdb_backend as duckdb_backend
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit
from verinote.pipeline.query import query_path
from verinote.pipeline.verify import load_policy, policy_path, verify
from verinote.store import Store
from verinote.store.duckdb_fact_terms import fact_terms_path
from verinote.store.fact_input import structural_term

# A hand-written policy tracked in the repo (see tests/test_gitignore.py), used
# here exactly as verinote loads a KB's own `policy/logic-policy.dl`. Such a
# policy is authored by hand and unreproducible — a bare `*.dl` ignore rule used
# to swallow files like this, keeping them out of git, which is how a KB owner's
# rules get lost for good. What verify() does when the policy is *absent* is a
# separate question (see #155); this fixture pins the other half: a policy that
# is present is really the one that runs.
SAMPLE_POLICY_FIXTURE = Path(__file__).parent / "fixtures" / "policy" / "sample-policy.dl"


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_verify_gates_on_contradiction_with_default_policy(tmp_path):
    s = _store(tmp_path)
    # two distinct established_on values for one subject = functional conflict
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "established_on", "2021", status="confirmed")
    rep = verify(s)
    assert rep.errors > 0 and rep.ok is False
    assert "backend: DuckDB" in rep.text


def test_verify_gates_on_functional_conflict_across_term_kinds(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Ada", "born_on", "1815", status="confirmed")
    s.add_fact("Ada", Atom("born_on"), NumberLit(1900), status="confirmed")

    rep = verify(s)

    assert rep.ok is False
    assert "ERROR functional_conflict: Ada born_on" in rep.findings


def test_verify_consistent_kb_passes(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "is_a", "company", status="accepted")
    rep = verify(s)
    assert rep.errors == 0 and rep.ok is True


def test_verify_only_considers_engine_statuses(tmp_path):
    s = _store(tmp_path)
    # a contradicting pair that is NOT yet confirmed must not gate
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "established_on", "2021", status="needs_review")
    rep = verify(s)
    assert rep.errors == 0 and rep.ok is True


def test_verify_loads_kb_policy_file(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    p = policy_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_has_isa(subject: symbol, object: symbol)\n"
        'error_has_isa(S, O) :- relation(S, "is_a", O).\n',
        encoding="utf-8",
    )
    rep = verify(s)
    assert rep.errors == 1
    assert "has_isa: Org company" in "\n".join(rep.findings)


def test_verify_loads_hand_written_fixture_policy(tmp_path):
    """The tracked sample policy is loaded for real, not just shipped alongside."""
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "established_on", "2021", status="confirmed")
    p = policy_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SAMPLE_POLICY_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    rep = verify(s)

    assert load_policy(s) == SAMPLE_POLICY_FIXTURE.read_text(encoding="utf-8")
    assert rep.ok is False
    assert rep.errors == 1
    # `warn_fixture_policy_loaded` exists only in the fixture: the shipped default
    # policy cannot produce it, so this pins that the KB's own policy was used.
    assert "WARN fixture_policy_loaded: Org" in rep.findings
    assert "ERROR functional_conflict: Org established_on" in rep.findings


def test_verify_loads_query_file(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Ada", "born_in", "London", status="confirmed")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ["q1: London"]
    assert "--- query input ---" in rep.text


def test_verify_answers_query_through_relation_alias(tmp_path):
    s = _store(tmp_path)
    s.add_fact("샘플인물", "역할", "샘플역할", status="confirmed")
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(V) :- relation("샘플인물", "role", V).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ["q1: 샘플역할"]


def test_verify_answers_explicit_korean_role_query(tmp_path):
    """Asking in a raw label sees every fact that label's canonical covers (#238).

    The default aliases map both `역할` and `대표` to `role`, so under this KB's
    own policy the two facts state the same relation and both answer the
    question. Asking canonically (`"role"`) already returned both — alias
    expansion appends a rule per raw label — so before the engine read facts
    through the aliases, the same question got different answers depending on
    which spelling it was asked in. Unrelated relations (`발표자`) still do not
    answer.
    """
    s = _store(tmp_path)
    s.add_fact("샘플인물", "역할", "샘플역할", status="confirmed")
    s.add_fact("샘플인물", "대표", "샘플조직", status="confirmed")
    s.add_fact("샘플기관", "발표자", "샘플인물", status="confirmed")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(O) :- relation("샘플인물", "역할", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ["q1: 샘플역할, 샘플조직"]


def test_verify_answers_query_through_multiple_relation_aliases(tmp_path):
    s = _store(tmp_path)
    s.add_fact("샘플인물", "역할", "샘플과제", status="confirmed")
    s.add_fact("샘플과제", "직함", "샘플역할", status="confirmed")
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        "- `role` -> `역할`\n- `title` -> `직함`\n",
        encoding="utf-8",
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(V) :- relation("샘플인물", "role", X), relation(X, "title", V).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ["q1: 샘플역할"]


def test_verify_reports_invalid_relation_alias_policy(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `role`\n", encoding="utf-8")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\n'
        'answer_q1(V) :- relation("샘플인물", "role", V).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is False
    assert rep.errors == 1
    assert "policy error" in rep.findings[0]


def test_verify_uses_duckdb_fact_terms_for_structural_compounds(tmp_path):
    s = _store(tmp_path)
    s.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="confirmed",
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation(person("Ada"), has_role, O).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ['q1: role(person("Ada"), "PI")']


def test_verify_ignores_candidate_structural_terms(tmp_path):
    s = _store(tmp_path)
    s.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="candidate",
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(S) :- relation(S, has_role, role(person("Ada"), "PI")).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert "--- fact input ---\n(none)" in rep.text


def test_verify_keeps_plain_term_syntax_as_stringlit(tmp_path):
    s = _store(tmp_path)
    s.add_fact('person("Ada")', "has_role", 'role(person("Ada"), "PI")', status="confirmed")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation(person("Ada"), "has_role", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert 'relation("person(\\"Ada\\")", "has_role", "role(person(\\"Ada\\"), \\"PI\\")")' in rep.text
    assert 'relation(person("Ada"), "has_role", role(person("Ada"), "PI"))' not in rep.text


def test_verify_debug_fact_input_is_quoted_and_escaped(tmp_path):
    s = _store(tmp_path)
    s.add_fact('a"b', "r", "line\nnext", status="confirmed")

    rep = verify(s)

    assert 'relation("a\\"b", "r", "line\\nnext")' in rep.text


def test_verify_backfills_missing_duckdb_terms_for_legacy_engine_rows(tmp_path):
    s = _store(tmp_path)
    cur = s._conn.execute(
        "INSERT INTO facts(subject, relation, object, status) VALUES(?,?,?,?) RETURNING id",
        ("Ada", "born_in", "London", "confirmed"),
    )
    fid = int(cur.fetchone()[0])
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n',
        encoding="utf-8",
    )

    rep = verify(s)

    assert rep.ok is True
    assert rep.answers == ["q1: London"]
    assert s.get_fact_terms(fid) == (
        StringLit("Ada"),
        StringLit("born_in"),
        StringLit("London"),
    )


def test_verify_rejects_lost_sidecar_instead_of_backfilling_structural_terms(tmp_path):
    s = _store(tmp_path)
    s.add_fact(
        Compound("org", (StringLit("demo"),)),
        "ceo",
        StringLit("Alice"),
        status="confirmed",
    )
    s.add_fact(
        Compound("org", (StringLit("demo"),)),
        "ceo",
        StringLit("Bob"),
        status="confirmed",
    )
    path = policy_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_two_ceos(object: symbol)\n"
        'error_two_ceos(O) :- relation(org("demo"), "ceo", O), '
        'relation(org("demo"), "ceo", P), O != P.\n',
        encoding="utf-8",
    )
    assert verify(s).ok is False
    s.close()
    fact_terms_path(tmp_path).unlink()

    reopened = Store(tmp_path / "kb.sqlite")
    try:
        rep = verify(reopened)
    finally:
        reopened.close()

    assert rep.ok is False
    assert rep.errors == 1
    assert "missing DuckDB fact terms" in rep.text
    assert "Refusing to rebuild" in rep.text
    assert "knowledge base is consistent" not in rep.text


def test_verify_fails_closed_when_engine_fact_terms_remain_missing(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s._conn.execute(
        "INSERT INTO facts(subject, relation, object, status) VALUES(?,?,?,?)",
        ("Ada", "born_in", "London", "confirmed"),
    )
    monkeypatch.setattr(s, "backfill_fact_terms", lambda: 0)

    rep = verify(s)

    assert rep.ok is False
    assert rep.errors == 1
    assert "missing DuckDB fact terms" in rep.text


def test_verify_fails_closed_when_duckdb_fact_terms_are_stale(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("Ada", "born_in", "London", status="confirmed")
    s._conn.execute(
        "UPDATE facts SET object = ?, term_token = ? WHERE id = ?",
        ("Paris", "0" * 64, fid),
    )

    rep = verify(s)

    assert rep.ok is False
    assert rep.errors == 1
    assert "stale DuckDB fact terms" in rep.text


def test_verify_reports_missing_duckdb_as_blocking(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    s = _store(tmp_path)

    rep = verify(s)

    assert rep.engine_available is False
    assert rep.ok is False
    assert rep.errors == 1
    assert "backend: DuckDB" in rep.text
    assert "DuckDB is not installed" in rep.text


def test_verify_reuses_store_inference_cache(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    loads = []
    real_load = duckdb_backend._load_relation_facts

    def counted_load(con, facts):
        loads.append(list(facts))
        return real_load(con, facts)

    monkeypatch.setattr(duckdb_backend, "_load_relation_facts", counted_load)

    assert verify(s).ok is True
    assert verify(s).ok is True

    assert len(loads) == 1


def test_store_close_clears_inference_cache(tmp_path):
    s = _store(tmp_path)
    _ = s.inference_cache

    assert s._inference_cache is not None
    s.close()

    assert s._inference_cache is None


def test_verify_candidate_change_does_not_enter_or_reload_cached_relation(
    tmp_path, monkeypatch
):
    s = _store(tmp_path)
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    loads = []
    real_load = duckdb_backend._load_relation_facts

    def counted_load(con, facts):
        loads.append(list(facts))
        return real_load(con, facts)

    monkeypatch.setattr(duckdb_backend, "_load_relation_facts", counted_load)

    assert verify(s).ok is True
    s.add_fact("Org", "established_on", "2021", status="candidate")
    rep = verify(s)

    assert rep.ok is True
    assert "2021" not in rep.text
    assert len(loads) == 1


def test_verify_cache_refreshes_after_status_promotion_and_demotion(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    conflict = s.add_fact("Org", "established_on", "2021", status="needs_review")

    assert verify(s).ok is True
    s.set_status(conflict, "confirmed")
    rep = verify(s)
    assert rep.ok is False
    assert "functional_conflict" in "\n".join(rep.findings)

    s.set_status(conflict, "superseded")
    assert verify(s).ok is True


def test_verify_cache_refreshes_after_engine_fact_amendment(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    conflict = s.add_fact("Org", "established_on", "2021", status="confirmed")

    assert verify(s).ok is False
    s.amend_fact(
        conflict,
        subject="Org",
        relation="established_on",
        obj="2020",
    )

    assert verify(s).ok is True


def test_verify_cache_refreshes_after_approved_fact_term_change(tmp_path):
    s = _store(tmp_path)
    fid = s.add_fact("Ada", "born_in", "London", status="confirmed")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n',
        encoding="utf-8",
    )

    assert verify(s).answers == ["q1: London"]
    s.amend_fact(fid, subject="Ada", relation="born_in", obj="Paris")

    assert verify(s).answers == ["q1: Paris"]


def test_verify_cache_does_not_reload_for_candidate_term_payload_change(
    tmp_path, monkeypatch
):
    s = _store(tmp_path)
    s.add_fact("Ada", "born_in", "London", status="confirmed")
    candidate = s.add_fact(
        Compound("person", (StringLit("Ada"),)),
        "has_role",
        "PI",
        status="candidate",
    )
    loads = []
    real_load = duckdb_backend._load_relation_facts

    def counted_load(con, facts):
        loads.append(list(facts))
        return real_load(con, facts)

    monkeypatch.setattr(duckdb_backend, "_load_relation_facts", counted_load)

    assert verify(s).ok is True
    s.fact_terms.put_fact_terms(candidate, Compound("person", (StringLit("Grace"),)), "has_role", "PI")
    assert verify(s).ok is True

    assert len(loads) == 1


def test_verify_cached_engine_does_not_leak_removed_query_answers(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Ada", "born_in", "London", status="confirmed")
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n',
        encoding="utf-8",
    )

    assert verify(s).answers == ["q1: London"]
    path.unlink()

    rep = verify(s)
    assert rep.answers == []
    assert "--- answers ---" not in rep.text


def test_verify_cached_engine_does_not_leak_changed_policy_findings(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Ada", "is_a", "person", status="confirmed")
    path = policy_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_has_isa(subject: symbol)\n"
        'warn_has_isa(S) :- relation(S, "is_a", O).\n',
        encoding="utf-8",
    )

    assert verify(s).findings == ["WARN has_isa: Ada"]
    path.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n",
        encoding="utf-8",
    )

    assert verify(s).findings == []
