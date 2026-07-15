# SPDX-License-Identifier: MPL-2.0
"""The engine must see facts through the KB's relation aliases.

Aliases were parsed for extraction, query planning and trust, but the rows that
reach the logic engine came straight from the fact-term sidecar. So a KB could
declare `설립 -> established_on` and still have two contradicting `설립` dates
pass `verify()` clean: the policy says `functional("established_on")` and the
engine never saw that label. Adding more aliases cannot fix that — the alias
layer simply was not on the engine's input path. These tests pin the path, not
a vocabulary list.
"""

from pathlib import Path

import pytest

from verinote.engine import DEFAULT_POLICY, compile_dl, run_check
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.terms import StringLit
from verinote.pipeline.engine_input import engine_relation_rows
from verinote.pipeline.query import query_path
from verinote.pipeline.report_trace import report_trace
from verinote.pipeline.verify import policy_path, verify
from verinote.policy_defaults import RELATION_ALIASES_RELPATH
from verinote.store import Store

_FUNCTIONAL_POLICY = """\
.decl relation(subject: symbol, rel: symbol, object: symbol)
.decl functional(rel: symbol)
functional("{relation}").
.decl error_functional_conflict(subject: symbol, rel: symbol)
error_functional_conflict(S, R) :-
    relation(S, R, A), relation(S, R, B), functional(R), A != B.
"""


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _write_policy(store: Store, text: str) -> None:
    path = policy_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_aliases(store: Store, text: str) -> None:
    path = Path(store.db_path).parent / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_verify_gates_on_english_established_alias(tmp_path):
    """#238 acceptance: `established` is an alias of the functional relation."""
    s = _store(tmp_path)
    s.add_fact("Org", "established", "2020", status="accepted")
    s.add_fact("Org", "established", "2021", status="accepted")

    rep = verify(s)

    assert rep.errors == 1
    assert rep.ok is False


def test_verify_gates_on_korean_established_alias(tmp_path):
    s = _store(tmp_path)
    s.add_fact("회사", "설립", "2020", status="accepted")
    s.add_fact("회사", "설립", "2021", status="accepted")

    rep = verify(s)

    assert rep.errors == 1
    assert rep.ok is False


def test_verify_honors_arbitrary_kb_alias_the_defaults_never_mention(tmp_path):
    """The hole no vocabulary list can close: a KB's own alias must reach the engine.

    Nothing here is hard-coded anywhere in verinote — `foo -> bar` exists only in
    this KB's alias file. If the engine input path did not apply aliases, this
    stays green forever no matter how many date words the defaults grow.
    """
    s = _store(tmp_path)
    _write_aliases(s, "- `foo` -> `bar`\n")
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="bar"))
    s.add_fact("Subject", "foo", "one", status="accepted")
    s.add_fact("Subject", "foo", "two", status="accepted")

    rep = verify(s)

    assert rep.errors == 1
    assert rep.ok is False


def test_engine_rows_are_canonical_while_stored_labels_stay_raw(tmp_path):
    s = _store(tmp_path)
    fact_id = s.add_fact("회사", "설립", "2020", status="accepted")

    rows = engine_relation_rows(s)

    assert [row["relation"] for row in rows] == [StringLit("established_on")]
    assert [row["relation_raw"] for row in rows] == [StringLit("설립")]
    # Read-time normalization only: the KB still records what the source said.
    assert s.get_fact(fact_id)["relation"] == "설립"
    assert s.get_fact_terms(fact_id)[1] == StringLit("설립")


def test_raw_label_queries_still_answer_and_report_the_source_label(tmp_path):
    """Canonicalizing engine input must not strand queries written in the raw label."""
    s = _store(tmp_path)
    s.add_fact("회사", "설립", "2020", status="accepted")
    query_path(Path(s.db_path).parent).parent.mkdir(parents=True, exist_ok=True)
    query_path(Path(s.db_path).parent).write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("회사", "설립", O).\n',
        encoding="utf-8",
    )

    trace = report_trace(s)

    assert [(answer.qid, answer.value) for answer in trace.answers] == [("1", "2020")]
    # What a human reads is the label the source used, not the policy's canonical.
    assert [fact.relation for fact in trace.answers[0].facts] == ["설립"]


def test_nothing_outside_the_normalizer_reads_engine_fact_terms():
    """Drift guard: one normalization point, or the aliases silently stop applying.

    A `store.engine_fact_terms()` call anywhere else — a CLI command, a report,
    a new pipeline stage — is a second, un-normalized engine input path, which is
    exactly the bug this fixes. So the whole package is scanned, not `pipeline/`:
    the accessor's definition in `store/db.py` and its one caller are the only
    places it may appear.
    """
    package = Path(__file__).resolve().parents[1] / "verinote"
    allowed = {
        package / "pipeline" / "engine_input.py",
        package / "store" / "db.py",
    }
    offenders = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path not in allowed
        and "engine_fact_terms(" in path.read_text(encoding="utf-8")
    )

    assert offenders == []


def test_duckdb_backend_sees_the_canonical_relation(tmp_path):
    """The operational path: a `설립` conflict reaches the `established_on` policy.

    DuckDB is verinote's production engine, so this runs everywhere, including CI
    without pyrewire — the wirelog half is a separate, guarded test below.
    """
    s = _store(tmp_path)
    s.add_fact("회사", "설립", "2020", status="accepted")
    s.add_fact("회사", "설립", "2021", status="accepted")

    duck = run_check_duckdb(engine_relation_rows(s), policy_dl=DEFAULT_POLICY)

    assert duck.errors == 1


def test_wirelog_backend_sees_the_canonical_relation(tmp_path):
    """Both engines read one canonical input, so they cannot disagree about it.

    The wirelog engine only runs when pyrewire is installed; without it,
    `run_check` returns an `engine_available=False` compatibility report, so the
    assertion would be meaningless. Skipping without pyrewire matches every other
    wirelog test (`pytest.importorskip`), and CI runs without it (see #234).
    """
    pytest.importorskip("pyrewire")
    s = _store(tmp_path)
    s.add_fact("회사", "설립", "2020", status="accepted")
    s.add_fact("회사", "설립", "2021", status="accepted")

    rows = engine_relation_rows(s)
    wire = run_check(
        compile_dl(
            [
                {key: _text(row[key]) for key in ("subject", "relation", "object")}
                for row in rows
            ]
        ),
        policy_dl=DEFAULT_POLICY,
    )

    assert wire.errors == 1


def test_default_aliases_do_not_merge_relations_into_a_false_conflict(tmp_path):
    """Aliasing distinct relations together manufactures conflicts that do not exist.

    Once the engine reads facts through the aliases, an alias is a claim that two
    labels are the same relation — so a wrong alias is no longer harmless. Holding
    the title PI and representing Acme is not a contradiction, and the default
    table must not turn it into one by sending `역할` and `대표` to the same
    functional `role`.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="role"))
    s.add_fact("김철수", "역할", "PI", status="accepted")
    s.add_fact("김철수", "대표", "Acme", status="accepted")

    rep = verify(s)

    assert rep.errors == 0
    assert rep.ok is True


def test_findings_name_the_conflicting_facts_in_the_source_s_own_words(tmp_path):
    """A report about `established_on` is unreadable to a KB that only said `설립`."""
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    first = s.add_fact("회사", "설립", "2020", status="accepted")
    second = s.add_fact("회사", "founded", "2021", status="accepted")

    rep = verify(s)

    assert rep.findings == [
        f"ERROR functional_conflict: 회사 established_on "
        f"(설립 #{first}=2020, founded #{second}=2021)"
    ]
    assert rep.findings[0] in rep.text


def test_findings_for_unaliased_relations_are_left_alone(tmp_path):
    """Only a renamed relation needs its source labels restored."""
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    s.add_fact("회사", "established_on", "2020", status="accepted")
    s.add_fact("회사", "established_on", "2021", status="accepted")

    rep = verify(s)

    assert rep.findings == ["ERROR functional_conflict: 회사 established_on"]


def _text(term: object) -> str:
    assert isinstance(term, StringLit)
    return term.value
