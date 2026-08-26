# SPDX-License-Identifier: MPL-2.0
r"""`GET /questions` and `GET /report` degrade on an unreadable
`policy/relation-aliases.md` instead of 500ing (#590).

This is the web-only half of #570's deferral; #591 was the CLI-shared half.
Read `tests/test_query_schema_policy_guard.py` (#591) and
`tests/test_typed_relations_web_guard.py` (#585) first -- this file extends
their constants and reasons rather than restating them.

TWO BROKEN INPUT CLASSES, AND MALFORMED IS NOT ONE OF THEM. Unlike #591's call
site, these two routes already survive a malformed alias file: the narrow
`except CorroborationPolicyError` clauses in `verify.py` and `report_trace.py`
catch the PARSE class and turn it into a proper `policy_error` report. Measured
on `c0dd1cc`, both routes:

    healthy                 200      malformed (parse error)      200
    self-map (parse error)  200      is-a-directory               200
    cp949 (decode)          500      chmod 000 (PermissionError)  500
    broken typed file       200

So the guard exists for exactly two classes, and both are here. A cp949-only
suite would leave the `PermissionError` half unpinned -- and that half is
precisely what #555's G2 rationale was written for: `PermissionError` is not a
`ValueError` at all, so an `except ValueError` that looks right would miss it.
`ALIAS_MALFORMED` appears below as a case that must STAY 200 with its report
unchanged, never as a broken input.

THE ALIAS FILE ONLY -- AND WHY NOT `query_schema_policy_failure`. #591 made that
function the established entry point, and using it here would be wrong: it
checks BOTH policy files, and `verify.py` reads only the alias one
(`store_typed_relations` appears nowhere in it), so routing these routes through
the two-file check would withhold a report for a file `verify()` never read.
The guard calls `policy_file_failure` with the alias reader alone.
`test_a_broken_typed_file_does_not_trigger_this_guard_on_questions` is what
stops that simplification.

**Do not read that as "these routes never touch the typed file."** An earlier
draft of this very docstring said exactly that, twenty lines above the paragraph
below recording the measured opposite. `/report` also calls `report_trace`,
which reaches `fact_trust_summary`, which reads BOTH files. The true claim is
about `verify.py`; generalising it to the routes is what made it false, and the
true half is what made it read as checked.

THE THREE NARROW CLAUSES ARE LIVE AND MUST NOT BE DELETED. An earlier revision
of this change's plan called them dead code. They are not:
`query.py::expand_query_relation_aliases` raises `CorroborationPolicyError` when
a rule's alias expansion exceeds `MAX_ALIAS_EXPANDED_RULES_PER_RULE`, and that
is reachable on a KB where BOTH policy files are valid -- three raw labels
mapping to one canonical name and a draft rule with four `relation/3` atoms is
4**4 = 256 against a cap of 64. Deleting the clauses would turn that valid
configuration into a 500. `test_the_alias_expansion_cap_still_answers` pins it,
and it is not a new guard: it is an existing one this change must not break.

A DEFECT THIS CHANGE FOUND AND DID NOT FIX, SINCE FIXED BY #595. `report_trace`
reaches `fact_trust_summary`, which reads BOTH policy files, so on a KB whose
report had a traceable answer a broken `policy/typed-relations.md` MADE
`GET /report` answer 500 -- measured here and identically on this change's base
commit, so it predated this work and was left to #595, which guards it in the
route. It answers 200 now.

The reason it went unseen is the part worth keeping: it was invisible to any
sweep whose fixture had no traceable answer, which is how #585 missed it and how
this change's own plan concluded these two routes never read the typed file.
`/questions` was unaffected throughout: it does not call `report_trace`.

WHAT DEGRADES, AND WHAT DOES NOT. `/questions` is `/review`'s shape: the queue
and the repair job come from the store, read no policy file, and stay; only
`answers` is withheld, and it is rendered AS withheld rather than as an empty
list, which would read as an engine run that found nothing. `/report` is
`/workbench`'s shape: `rep` and `trace` are both alias-derived, so there is no
half of the page that survives, and it says not-computed rather than rendering a
finding-free report -- a positive claim that the KB checked clean.
"""

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from verinote.config import Config
from verinote.pipeline.report_trace import report_trace
from verinote.policy_defaults import RELATION_ALIASES_RELPATH, TYPED_RELATIONS_RELPATH
from verinote.pipeline.query import MAX_ALIAS_EXPANDED_RULES_PER_RULE
from verinote.pipeline.verify import verify
from verinote.web import create_app

ALIAS_HEALTHY = "- 자본금 -> capital_of\n".encode()
ALIAS_CP949 = "- 자본금 -> capital_of\n".encode("cp949")
# Parse error. NOT a broken input here -- it is 200 today and must stay 200.
ALIAS_MALFORMED = "- 자본금 capital_of\n".encode()
TYPED_HEALTHY = "- 자본금: amount as capital\n".encode()
TYPED_DUP_ALIAS = "- 자본금: amount as capital\n- 자산: amount as capital\n".encode()

ALIAS_MSG = "relation-aliases.md"
NOT_COMPUTED = "Not computed — see the policy-file notice above."
# The two empty-state sentences that must never stand over a run that did not happen.
QUESTIONS_HEALTHY_EMPTY = "No engine answers yet"
REPORT_HEALTHY_EMPTY = "No direct relation fact traces are available"
THE_ANSWER = "q1: 1억"

# The query asks about the CANONICAL relation; the fact is stored under the RAW
# label. Only an applied alias file connects them, which is what makes every
# control here non-vacuous -- verified by diffing the rendered bodies with and
# without the file before any assertion below was written. Without the `.decl`
# the engine reports `unknown predicate` and BOTH builds render identically,
# which is how the first version of this fixture proved nothing.
QUERY_DL = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(O) :- relation("A", "capital_of", O).\n'
)


def _build(tmp_path: Path, *, alias: bytes | None, typed: bytes = TYPED_HEALTHY,
           query_dl: str = QUERY_DL, raw_relation: str = "자본금"):
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    cfg = Config(root=root, db_path=root / "kb.sqlite", provider="anthropic",
                 model="m", api_key=None, base_url=None)
    app = create_app(cfg)
    store = app.state.store
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "a.txt").write_text("x\n", encoding="utf-8")
    source_id = store.add_source("sources/a.txt")
    store.add_fact("A", raw_relation, "1억", status="confirmed", confidence=0.9,
                   source_id=source_id)
    store.add_question("A의 자본금은?")
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if alias is not None:
        path.write_bytes(alias)
    (root / TYPED_RELATIONS_RELPATH).write_bytes(typed)
    query_path = root / "facts" / "query.dl"
    query_path.parent.mkdir(parents=True, exist_ok=True)
    query_path.write_text(query_dl, encoding="utf-8")
    return TestClient(app, raise_server_exceptions=False), app, path


@pytest.fixture
def unreadable_client(tmp_path):
    """A well-formed alias file at mode 0o000 -- `PermissionError`, which is not
    a `ValueError` and so escapes every narrow clause.

    Skips rather than passing vacuously if the mode does not actually deny the
    read (running as root), because a test that cannot reach its own failure
    mode is green for the wrong reason.
    """
    client, app, path = _build(tmp_path, alias=ALIAS_HEALTHY)
    os.chmod(path, 0o000)
    try:
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("this process can read a 0o000 file; the mode denies nothing here")
        yield client
    finally:
        os.chmod(path, 0o644)


BROKEN = [
    pytest.param(ALIAS_CP949, id="cp949-decode"),
    pytest.param(None, id="permission-denied"),  # None => use the unreadable fixture
]


# ---------------------------------------------------------------------------
# AC-1 / AC-2 -- both routes survive both broken classes, and say which file
# ---------------------------------------------------------------------------


def test_questions_survives_an_undecodable_alias_file(tmp_path):
    """The assertion is the MESSAGE, not the status. #585 measured that deleting
    the narrow clause in front of the broad one leaves every status unchanged
    and reddens 25 tests with ZERO status assertions among them."""
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    r = client.get("/questions")
    assert r.status_code == 200
    assert ALIAS_MSG in r.text


def test_report_survives_an_undecodable_alias_file(tmp_path):
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    r = client.get("/report")
    assert r.status_code == 200
    assert ALIAS_MSG in r.text


def test_questions_survives_an_unreadable_alias_file(unreadable_client):
    """The `PermissionError` half. `except ValueError` would look right and miss
    this one entirely."""
    r = unreadable_client.get("/questions")
    assert r.status_code == 200
    assert ALIAS_MSG in r.text


def test_report_survives_an_unreadable_alias_file(unreadable_client):
    r = unreadable_client.get("/report")
    assert r.status_code == 200
    assert ALIAS_MSG in r.text


# ---------------------------------------------------------------------------
# The parse class must be untouched by the pre-flight
# ---------------------------------------------------------------------------


def test_a_malformed_alias_file_still_reports_the_same_policy_error(tmp_path):
    """Malformed is 200 today and must stay 200 with the same report. The
    pre-flight returns the narrow clause's exact shape, code and message for
    this class, so the outcome is unchanged rather than merely still-200."""
    client, app, _ = _build(tmp_path, alias=ALIAS_MALFORMED)
    rep = verify(app.state.store)
    assert (rep.ok, rep.errors) == (False, 1)
    assert [d.code for d in rep.finding_details] == ["policy_error"]
    for route in ("/questions", "/report"):
        assert client.get(route).status_code == 200


def test_the_alias_expansion_cap_still_answers(tmp_path):
    """THE ANTI-DELETION GUARD. Both policy files are valid here; what raises is
    `expand_query_relation_aliases`'s cap, from inside `load_query`. The three
    narrow clauses that catch it are live, and an earlier plan for this very
    change called them dead code and said to delete them -- which would have
    turned this valid configuration into a 500.

    Red the moment `verify.py`'s second clause, `report_trace.py`'s, or
    `app.py`'s `_questions` clause is removed.
    """
    aliases = "".join(f"- r{i} -> R\n" for i in range(1, 4)).encode()
    atoms = ", ".join(['relation(_, "R", _)'] * 4)
    client, app, _ = _build(
        tmp_path,
        alias=aliases,
        query_dl=f".decl answer_q1(value: symbol)\nanswer_q1(S) :- relation(S, \"R\", _), {atoms}.\n",
        raw_relation="r1",
    )
    assert 4 ** 4 > MAX_ALIAS_EXPANDED_RULES_PER_RULE
    for route in ("/questions", "/report"):
        r = client.get(route)
        assert r.status_code == 200
        assert "exceeds" in r.text, f"{route} lost the cap diagnosis"


# ---------------------------------------------------------------------------
# AC-2 -- withheld, and withheld visibly
# ---------------------------------------------------------------------------


def test_questions_keeps_the_queue_it_can_still_read(tmp_path):
    """`/review`'s shape: the store-derived queue survives, only `answers` is
    withheld, and it is rendered as withheld rather than as an empty list."""
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    body = client.get("/questions").text
    assert "A의 자본금은?" in body           # the queue is still the KB's own
    assert NOT_COMPUTED in body              # and the answers say why they are gone
    assert QUESTIONS_HEALTHY_EMPTY not in body
    assert THE_ANSWER not in body


def test_report_does_not_render_a_finding_free_report(tmp_path):
    """`/workbench`'s shape: no half of this page survives, so it must not print
    the traceability empty-state, which reads as a search that found nothing."""
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    body = client.get("/report").text
    assert NOT_COMPUTED in body
    assert REPORT_HEALTHY_EMPTY not in body
    assert THE_ANSWER not in body


REPORT_BANNER = "The report and its traceability are"


def test_the_report_banner_says_what_was_withheld(tmp_path):
    """`report.html`'s banner, pinned on its own sentence.

    It was unpinned when this file was written, and the reason is worth keeping:
    it is NOT the sole carrier of the file NAME. `verify()`'s pre-flight puts the
    message into `rep.findings` and `rep.text`, which the page renders anyway, so
    `test_the_banner_claims_nothing_about_a_file_it_did_not_read` passed with the
    whole `{% if policy_error %}` block deleted. A test named for the banner did
    not test the banner. What the block IS sole carrier of is the explanation of
    WITHHELD-NESS -- that the report and trace are missing on purpose rather than
    empty -- so that is what this asserts.
    """
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    body = client.get("/report").text
    assert REPORT_BANNER in body
    assert ALIAS_MSG in body


def test_a_healthy_alias_file_still_answers(tmp_path):
    """Anti-vacuity for every test above: a guard that fired unconditionally
    would pass all of them. The fixture's answer exists only because the alias
    file connects a fact stored under `자본금` to a query asking `capital_of`."""
    client, _, _ = _build(tmp_path, alias=ALIAS_HEALTHY)
    for route in ("/questions", "/report"):
        body = client.get(route).text
        assert THE_ANSWER in body, f"{route} lost the fixture's answer"
        assert NOT_COMPUTED not in body
        assert ALIAS_MSG not in body


def test_the_fixture_answer_really_depends_on_the_alias_file(tmp_path):
    """The measurement the controls rest on. Without this, "the answer is
    withheld" could be pinning a value that was never there -- the failure mode
    #585 spent five revisions on."""
    with_file, _, _ = _build(tmp_path / "with", alias=ALIAS_HEALTHY)
    without, _, _ = _build(tmp_path / "without", alias=None)
    assert THE_ANSWER in with_file.get("/report").text
    assert THE_ANSWER not in without.get("/report").text


# ---------------------------------------------------------------------------
# Scope: the typed file is not this guard's business
# ---------------------------------------------------------------------------


def test_a_broken_typed_file_does_not_trigger_this_guard_on_questions(tmp_path):
    """Scope. `/questions` never reads `policy/typed-relations.md`, so a broken
    one must leave it fully intact -- answer included.

    Red if anyone "simplifies" the guard to `query_schema_policy_failure`, which
    checks BOTH files: that would withhold this page's answer for a file it
    never read. That is the most likely wrong turn here, because
    `query_schema_policy_failure` is #591's established entry point.
    """
    client, _, _ = _build(tmp_path, alias=ALIAS_HEALTHY, typed=TYPED_DUP_ALIAS)
    body = client.get("/questions").text
    assert NOT_COMPUTED not in body
    assert ALIAS_MSG not in body
    assert THE_ANSWER in body


def test_a_broken_typed_file_does_not_trigger_this_guard_on_report(tmp_path):
    """Same scope claim for `/report`, RE-WIDENED BY #595 to the fixture that can
    express it.

    THIS TEST USED THE WEAKER FIXTURE ON PURPOSE, and the narrowing is now
    resolved rather than merely explained. `report_trace` reaches
    `fact_trust_summary`, which reads BOTH policy files -- but only when there is
    an answer to trace. So on a KB with a traceable answer a broken
    `typed-relations.md` made `/report` answer 500, measured identically on
    `c0dd1cc` before #590, which is why it was out of #590's scope: asserting 200
    would have failed for an unrelated reason, and asserting 500 would have
    locked a defect in as desired behaviour. The fixture here therefore had no
    `query_dl`, and this docstring recorded that it could not pin
    `report_trace`'s own alias-only choice.

    #595 fixed the 500 in the route, so the full scope claim is assertable now
    and is asserted: an answer-bearing KB, a broken typed file, 200, and this
    guard's own message absent because the ALIAS file is healthy. The typed
    file's own diagnosis is `test_report_names_the_typed_file_and_not_the_alias_file`
    in `test_report_trace_typed_guard.py`; what belongs here is only that THIS
    guard stays quiet about a file it never read.
    """
    # The widening is only meaningful on a KB that HAS an answer to trace: with
    # no answer `report_trace` never reaches `fact_trust_summary`, the typed file
    # is never read, and the 200 below would prove nothing -- which is precisely
    # the state this test was left in. Pinned on the HEALTHY pair, because on the
    # broken one `report_trace` raises: that raise IS the defect #595 fixed, and
    # calling it directly there would measure the defect instead of the fixture.
    _, healthy_app, _ = _build(tmp_path / "healthy", alias=ALIAS_HEALTHY)
    assert len(report_trace(healthy_app.state.store).answers) >= 1

    client, _, _ = _build(tmp_path / "broken", alias=ALIAS_HEALTHY,
                          typed=TYPED_DUP_ALIAS)
    r = client.get("/report")
    assert r.status_code == 200
    assert ALIAS_MSG not in r.text


def test_the_banner_claims_nothing_about_a_file_it_did_not_read(tmp_path):
    """AC-5. The message names the alias file because the alias file is what
    failed, and says nothing at all about `typed-relations.md`."""
    client, _, _ = _build(tmp_path, alias=ALIAS_CP949)
    for route in ("/questions", "/report"):
        body = client.get(route).text
        assert ALIAS_MSG in body
        assert "typed-relations" not in body
