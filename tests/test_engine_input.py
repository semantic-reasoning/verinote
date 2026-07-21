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

from verinote.engine import (
    DEFAULT_POLICY,
    CheckReport,
    FindingRow,
    compile_dl,
    run_check,
)
from verinote.engine.duckdb_backend import run_check_duckdb
from verinote.engine.policy_vocabulary import (
    FUNCTIONAL_CONFLICT_COLUMNS,
    FUNCTIONAL_CONFLICT_RULE,
)
from verinote.engine.terms import StringLit, bare_label
from verinote.llm.base import ExtractedFact
from verinote.pipeline import extract_source
from verinote.pipeline.engine_input import annotate_source_labels, engine_relation_rows
from verinote.pipeline.query import query_path
from verinote.pipeline.report_trace import report_trace
from verinote.pipeline.verify import policy_path, verify
from verinote.pipeline.workbench import trust_workbench
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


# The rule identity an engine puts on a row it derived from verinote's own
# functional-conflict declaration — what a hand-built `FindingRow` needs to be
# the thing the annotator is willing to read.
_CONFLICT_SHAPE = (FUNCTIONAL_CONFLICT_RULE, FUNCTIONAL_CONFLICT_COLUMNS)


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


def test_raw_alias_labels_survive_extraction_and_drive_read_time(tmp_path, fake_client):
    """#252 end-to-end: extraction stores raw labels; read time canonicalizes them.

    The regression this closes shipped precisely because #250's tests injected raw
    labels with `add_fact`, bypassing extraction (the #241 mock gap). This drives
    the real extractor: a source using both `설립` and `founded` for the functional
    `established_on` relation must (1) be stored with those raw labels, (2) expose
    them under `relation_raw` while the engine sees the canonical relation, (3)
    surface the alias on the review badge, and (4) still be caught as one
    functional conflict, named in the source's own words.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    client = fake_client(
        [
            ExtractedFact("회사", "설립", "2020", 0.9),
            ExtractedFact("회사", "founded", "2021", 0.9),
        ]
    )

    extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="회사는 2020년에 설립되었고 2021년 기록도 있다",
    )

    # (1) stored with the raw labels, not canonicalized to established_on.
    stored = {(f["subject"], f["relation"], f["object"]) for f in s.facts()}
    assert stored == {("회사", "설립", "2020"), ("회사", "founded", "2021")}

    for fact in s.facts():
        s.accept_fact(int(fact["id"]))

    # (2) the engine sees the canonical relation; the raw label is preserved.
    rows = engine_relation_rows(s)
    assert {bare_label(row["relation"]) for row in rows} == {"established_on"}
    assert {bare_label(row["relation_raw"]) for row in rows} == {"설립", "founded"}

    # (3) the review badge fires — dead before the fix, when relation == canonical.
    wb = trust_workbench(s)
    badges = {
        fact.relation_alias
        for group in wb.conflicts
        for value in group.values
        for fact in value.facts
    }
    assert badges == {"설립 -> established_on", "founded -> established_on"}

    # (4) one functional conflict, named in the source's own words.
    rep = verify(s)
    assert rep.errors == 1
    conflict_findings = [f for f in rep.findings if "functional_conflict" in f]
    assert len(conflict_findings) == 1
    finding = conflict_findings[0]
    assert "established_on" in finding
    assert "설립" in finding and "founded" in finding


def test_within_source_alias_variants_store_twice_without_false_conflict(
    tmp_path, fake_client
):
    """#252 tradeoff: raw storage dedups per raw label, not per canonical relation.

    Within one source the model may emit both `설립` and `founded` for the same
    claim; they no longer merge at write time, so two candidate rows are stored.
    This is an accepted, deliberate cost, not a bug: at read time both canonicalize
    to `established_on` with the same value, so it is a redundant review candidate
    and never a false conflict.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    client = fake_client(
        [
            ExtractedFact("회사", "설립", "2020", 0.9),
            ExtractedFact("회사", "founded", "2020", 0.9),
        ]
    )

    extract_source(
        s,
        client,
        source_path="sources/x.txt",
        source_text="회사는 2020년에 설립되었다",
    )

    stored = {(f["subject"], f["relation"], f["object"]) for f in s.facts()}
    assert stored == {("회사", "설립", "2020"), ("회사", "founded", "2020")}

    for fact in s.facts():
        s.accept_fact(int(fact["id"]))

    rep = verify(s)
    assert rep.errors == 0
    assert rep.ok is True


def test_findings_name_canonical_and_aliased_facts_in_a_mixed_conflict(tmp_path):
    """A source note must include every fact that participates in the conflict.

    The note exists because at least one source label was renamed, but the
    conflict can be mixed: one fact may already use the canonical relation while
    another uses an alias. Limiting the note to renamed rows hides part of the
    conflict and makes the displayed provenance incomplete.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    canonical = s.add_fact("Org", "established_on", "2020", status="accepted")
    aliased = s.add_fact("Org", "설립", "2021", status="accepted")

    rep = verify(s)

    assert rep.findings == [
        f"ERROR functional_conflict: Org established_on "
        f"(established_on #{canonical}=2020, 설립 #{aliased}=2021)"
    ]
    assert rep.findings[0] in rep.text


def test_source_label_annotator_mixed_conflict_does_not_borrow_neighbors():
    line = "ERROR functional_conflict: Org established_on"
    report = CheckReport(
        ok=False,
        errors=1,
        warnings=0,
        text=line,
        findings=[line],
        finding_rows=[
            FindingRow(line, ("Org", "established_on"), *_CONFLICT_SHAPE),
        ],
    )
    rows = [
        {
            "id": 1,
            "subject": StringLit("Org"),
            "relation": StringLit("established_on"),
            "relation_raw": StringLit("established_on"),
            "object": StringLit("2020"),
        },
        {
            "id": 2,
            "subject": StringLit("Org"),
            "relation": StringLit("established_on"),
            "relation_raw": StringLit("설립"),
            "object": StringLit("2021"),
        },
        {
            "id": 3,
            "subject": StringLit("Other"),
            "relation": StringLit("established_on"),
            "relation_raw": StringLit("founded"),
            "object": StringLit("2030"),
        },
        {
            "id": 4,
            "subject": StringLit("Canonical"),
            "relation": StringLit("established_on"),
            "relation_raw": StringLit("established_on"),
            "object": StringLit("2040"),
        },
    ]

    annotate_source_labels(report, rows)

    assert report.findings == [
        "ERROR functional_conflict: Org established_on "
        "(established_on #1=2020, 설립 #2=2021)"
    ]


def test_source_label_annotator_treats_same_value_different_identity_as_ambiguous():
    line = "ERROR functional_conflict: Org f(x)"
    report = CheckReport(
        ok=False,
        errors=2,
        warnings=0,
        text=f"{line}\n{line}",
        findings=[line, line],
        finding_rows=[
            FindingRow(
                line,
                ("Org", "f(x)"),
                *_CONFLICT_SHAPE,
                ("s:Org", "c:f(A:x)"),
            ),
            FindingRow(
                line,
                ("Org", "f(x)"),
                *_CONFLICT_SHAPE,
                ("s:Org", "s:f(x)"),
            ),
        ],
    )
    rows = [
        {
            "id": 1,
            "subject": StringLit("Org"),
            "relation": StringLit("f(x)"),
            "relation_raw": StringLit("raw_compound"),
            "object": StringLit("2020"),
        },
        {
            "id": 2,
            "subject": StringLit("Org"),
            "relation": StringLit("f(x)"),
            "relation_raw": StringLit("raw_string"),
            "object": StringLit("2021"),
        },
    ]

    annotate_source_labels(report, rows)

    assert report.findings == [line, line]


def test_findings_do_not_borrow_an_overlapping_subject_s_facts(tmp_path):
    """A note must name the facts of *its* subject, not of every subject it contains.

    `Org` is a substring of `Org 2`, so matching a finding line by containment
    lets the `Org 2` conflict claim `Org`'s fact ids and values. Detection is
    unaffected — this is the report lying about which facts collided, which is
    the whole point of the note.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="established_on"))
    one = s.add_fact("Org", "설립", "2020", status="accepted")
    two = s.add_fact("Org", "founded", "2021", status="accepted")
    three = s.add_fact("Org 2", "설립", "2030", status="accepted")
    four = s.add_fact("Org 2", "founded", "2031", status="accepted")

    rep = verify(s)

    assert sorted(rep.findings) == sorted(
        [
            f"ERROR functional_conflict: Org established_on "
            f"(설립 #{one}=2020, founded #{two}=2021)",
            f"ERROR functional_conflict: Org\\ 2 established_on "
            f"(설립 #{three}=2030, founded #{four}=2031)",
        ]
    )


def test_findings_do_not_borrow_an_overlapping_relation_s_facts(tmp_path):
    """The same hole on the relation axis: `on` is a substring of `established_on`."""
    s = _store(tmp_path)
    _write_aliases(s, "- `설립` -> `established_on`\n- `켜짐` -> `on`\n")
    _write_policy(
        s,
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl functional(rel: symbol)\n"
        'functional("established_on").\n'
        'functional("on").\n'
        ".decl error_functional_conflict(subject: symbol, rel: symbol)\n"
        "error_functional_conflict(S, R) :-\n"
        "    relation(S, R, A), relation(S, R, B), functional(R), A != B.\n",
    )
    one = s.add_fact("Org", "설립", "2020", status="accepted")
    two = s.add_fact("Org", "설립", "2021", status="accepted")
    s.add_fact("Org", "켜짐", "yes", status="accepted")
    s.add_fact("Org", "켜짐", "no", status="accepted")

    rep = verify(s)

    established = [f for f in rep.findings if "established_on" in f]
    assert established == [
        f"ERROR functional_conflict: Org established_on "
        f"(설립 #{one}=2020, 설립 #{two}=2021)"
    ]


def test_a_finding_two_rows_rendered_alike_gets_no_note(tmp_path):
    """When a line is genuinely ambiguous, say nothing rather than guess.

    Values are joined bare, so subject `Org 2` + relation `x` and subject `Org`
    + relation `2 x` render to the identical line. It is one finding with no one
    row behind it; naming either row's facts would be a coin flip presented as
    provenance.
    """
    s = _store(tmp_path)
    s.add_fact("Org 2", "설립", "2020", status="accepted")
    rows = engine_relation_rows(s)
    line = "ERROR functional_conflict: Org 2 established_on"
    report = CheckReport(
        ok=False,
        errors=1,
        warnings=0,
        text=line,
        findings=[line],
        # Both render to `line`, and both are verinote's own functional-conflict
        # shape, so the tie is the only thing standing between this line and a
        # note: the first row would match the `Org 2` fact above, so resolving
        # the tie by arrival order would produce one.
        finding_rows=[
            FindingRow(line, ("Org 2", "established_on"), *_CONFLICT_SHAPE),
            FindingRow(line, ("Org", "2 established_on"), *_CONFLICT_SHAPE),
        ],
    )

    annotate_source_labels(report, rows)

    assert report.findings == [line]


def test_a_finding_from_a_rule_verinote_does_not_own_gets_no_note(tmp_path):
    """A note may only read columns whose meaning verinote declared itself.

    `error_mentions(S, O) :- relation(S, "mentions", O)` is a rule a user can
    write (#159), and its row is `(subject, object)` — no relation column at all.
    Here the object *is* the label `established_on`, so treating the row's values
    as an unordered bag finds "subject Org, relation established_on" and hands
    this finding the facts of an unrelated `설립` row. The finding is about
    `#2 Org mentions established_on`; nothing in it came from `#1`.
    """
    s = _store(tmp_path)
    _write_policy(
        s,
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_mentions(subject: symbol, object: symbol)\n"
        'error_mentions(S, O) :- relation(S, "mentions", O).\n',
    )
    s.add_fact("Org", "설립", "2020", status="accepted")
    s.add_fact("Org", "mentions", "established_on", status="accepted")

    rep = verify(s)

    assert rep.findings == ["ERROR mentions: Org established_on"]


def test_a_note_names_only_the_facts_of_the_conflict_s_own_subject(tmp_path):
    """A fact is named for its position in the row, not for being somewhere in it.

    A subject may be spelled like a relation: the KB below holds a `role`
    *subject* alongside the `role` conflict of subject `A`. Comparing the row's
    values as a bag makes `role/역할=Unrelated` match on the relation label while
    its subject matches on the subject label — and an unrelated fact is named as
    provenance for a conflict it is not part of.
    """
    s = _store(tmp_path)
    _write_policy(s, _FUNCTIONAL_POLICY.format(relation="role"))
    one = s.add_fact("A", "역할", "PI", status="accepted")
    two = s.add_fact("A", "역할", "Reviewer", status="accepted")
    s.add_fact("role", "역할", "Unrelated", status="accepted")

    rep = verify(s)

    assert rep.findings == [
        f"ERROR functional_conflict: A role (역할 #{one}=PI, 역할 #{two}=Reviewer)"
    ]


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
