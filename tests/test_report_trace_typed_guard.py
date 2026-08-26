# SPDX-License-Identifier: MPL-2.0
"""#595. `GET /report` degrades instead of 500ing when `typed-relations.md` is
broken AND the KB has a traceable answer.

THE FIXTURE IS THE ISSUE. `report_trace` only reaches `fact_trust_summary` --
the function that reads BOTH policy files -- when there is an answer to trace.
A KB with no traceable answer therefore cannot express this defect at all: it
answered 200 before the fix and 200 after, proving nothing. That is why #585 and
#590 both missed it, and why #590's T9 was deliberately narrowed to the weaker
fixture rather than asserting a number it could not justify.

So `test_the_fixture_has_a_traceable_answer` runs first and asserts the answer
exists. Every other test here is vacuous without it, and three attempts at this
fixture produced a 200 that proved nothing before the cause was found: the query
needs its own `.decl` line, or the engine reports `unknown predicate` and there
is no answer to trace.

WHY THE GUARD IS IN THE ROUTE AND NOT IN `report_trace`. Widening
`report_trace`'s alias-only pre-flight to check both files also removes the 500,
and it is measurably worse: the page answers 200, prints "No direct relation
fact traces are available for these report rows", and names no file -- a
searched-and-found-nothing verdict on a search that never ran. Measured, it also
costs the entire traceability table, not merely a banner. The route can say why;
an empty `ReportTrace` cannot carry a reason.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from verinote.config import Config
from verinote.pipeline.report_trace import report_trace
from verinote.policy_defaults import RELATION_ALIASES_RELPATH, TYPED_RELATIONS_RELPATH
from verinote.web.app import create_app

ALIAS_HEALTHY = "- 소속 -> member_of\n".encode()
ALIAS_CP949 = "- 소속 -> member_of\n".encode("cp949")
TYPED_HEALTHY = "- 자본금: amount as capital\n".encode()
# A duplicate alias -- one of the conditions `typed_relations` raises on; its own
# docstring enumerates them, and #589's derivation test keeps that list honest.
TYPED_BROKEN = "- 자본금: amount as capital\n- 자산: amount as capital\n".encode()

# The `.decl` line is load-bearing. Without it the engine reports `unknown
# predicate`, `answers` is empty, and every test below passes for the wrong
# reason -- which is exactly how this fixture failed three times before working.
QUERY_DL = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(O) :- relation("A", "member_of", O).\n'
)

# The BARE parser message, not the `policy/`-prefixed relpath. #590's G1 clause
# returns `str(exc)` for a `CorroborationPolicyError`, and the parser names its
# own file without the directory; the `policy/... could not be read` wrapper is
# G2, for failures that are not policy errors (a cp949 file, say). Asserting the
# relpath here fails against a page that is behaving correctly.
TYPED_NAMED = "typed-relations.md"
ALIAS_NAMED = "relation-aliases.md"
TRACE_WITHHELD = "The traceability below is withheld"
PAGE_WITHHELD = "The report and its traceability are withheld"
NOT_COMPUTED = "Not computed"
SEARCHED_AND_FOUND_NOTHING = "No direct relation fact traces"


def _build(tmp_path: Path, *, typed: bytes = TYPED_HEALTHY, alias: bytes = ALIAS_HEALTHY):
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    cfg = Config(root=root, db_path=root / "kb.sqlite", provider="anthropic",
                 model="m", api_key=None, base_url=None)
    app = create_app(cfg)
    store = app.state.store
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "a.txt").write_text("x\n", encoding="utf-8")
    source_id = store.add_source("sources/a.txt")
    store.add_fact("A", "소속", "B", status="confirmed", confidence=0.9,
                   source_id=source_id)
    alias_path = root / RELATION_ALIASES_RELPATH
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_bytes(alias)
    (root / TYPED_RELATIONS_RELPATH).write_bytes(typed)
    query_path = root / "facts" / "query.dl"
    query_path.parent.mkdir(parents=True, exist_ok=True)
    query_path.write_text(QUERY_DL, encoding="utf-8")
    return TestClient(app, raise_server_exceptions=False), store


def _flat(client) -> str:
    """The body with runs of whitespace collapsed.

    The banners wrap across source lines, so a contiguous needle does not match
    the raw text -- a probe written without this reported the whole-page banner
    ABSENT under a broken alias file, which would have been read as a #590
    regression rather than as a bad needle.
    """
    return " ".join(client.get("/report").text.split())


def test_the_fixture_has_a_traceable_answer(tmp_path):
    """AC-2, and it gates everything else in this file.

    If this fails, no other test here means anything: `report_trace` never
    reaches `fact_trust_summary` without an answer, so the defect cannot occur
    and a green suite says nothing about it.
    """
    _, store = _build(tmp_path)
    trace = report_trace(store)
    assert len(trace.answers) >= 1
    assert trace.answers[0].facts, "an answer with no contributing facts traces nothing"


def test_report_survives_a_broken_typed_file_with_a_traceable_answer(tmp_path):
    """AC-1. 500 before the fix, on this fixture and on `c0dd1cc` before #590."""
    client, _ = _build(tmp_path, typed=TYPED_BROKEN)
    assert client.get("/report").status_code == 200


def test_report_names_the_typed_file_and_not_the_alias_file(tmp_path):
    """AC-3. The banner names only the file that actually failed."""
    body = _flat(_build(tmp_path, typed=TYPED_BROKEN)[0])
    assert TYPED_NAMED in body
    # The bare name, which is the stronger negative: it would also catch the
    # `policy/`-prefixed wrapper form.
    assert ALIAS_NAMED not in body


def test_the_withheld_trace_does_not_claim_it_searched(tmp_path):
    """THE TEST THAT SEPARATES THIS FIX FROM THE WRONG ONE.

    Widening `report_trace`'s pre-flight also turns the 500 into a 200 and would
    pass every other test in this file. It renders "No direct relation fact
    traces are available for these report rows" -- a searched-and-found-nothing
    verdict about a search that never ran -- with no banner and no file named.
    This asserts the opposite of that page, so the wrong fix reddens here.

    SCOPED TO THE TRACE ARM ALONE, deliberately. It does NOT assert the banner:
    the banner is a separate arm of the template, and a version of this test
    that checked both reddened whenever EITHER was removed, leaving the trace
    arm with no test of its own. `test_report_names_the_typed_file_and_not_the_alias_file`
    and `test_the_report_itself_still_renders` carry the banner; this carries the
    sentence the trace section prints.
    """
    body = _flat(_build(tmp_path, typed=TYPED_BROKEN)[0])
    assert SEARCHED_AND_FOUND_NOTHING not in body
    assert NOT_COMPUTED in body


def test_the_report_itself_still_renders(tmp_path):
    """The trace is withheld; the report is NOT. `verify()` returns the same
    `ok=True, errors=0, answers=1` under a healthy and a broken typed file, so
    withholding the whole page would withhold correct numbers under a banner
    naming the wrong file. The page must not claim otherwise."""
    body = _flat(_build(tmp_path, typed=TYPED_BROKEN)[0])
    assert PAGE_WITHHELD not in body
    assert "the report itself is unaffected" in body


def test_a_broken_alias_file_still_withholds_the_whole_page(tmp_path):
    """#590 unchanged, and the ORDERING that keeps it honest.

    With the alias file broken the whole page is withheld and the typed file is
    neither read nor named -- naming it would be a claim about a file this
    request never needed. Both files are broken here on purpose: only the alias
    one may be named.
    """
    body = _flat(_build(tmp_path, typed=TYPED_BROKEN, alias=ALIAS_CP949)[0])
    assert PAGE_WITHHELD in body
    # A cp949 file is not a policy error, so this one arrives through G2's
    # wrapper and carries the full relpath -- a different shape from the typed
    # case above, and the reason both needles are spelled out rather than shared.
    assert RELATION_ALIASES_RELPATH in body
    assert TYPED_NAMED not in body
    assert TRACE_WITHHELD not in body


def test_a_broken_alias_file_does_not_even_read_the_typed_file(monkeypatch, tmp_path):
    """The ordering guard, pinned on the READ rather than on the banner.

    THE BANNER HALF IS ALREADY PROTECTED BY THE TEMPLATE, which is why this test
    is here: `report.html` renders `{% if policy_error %}` before
    `{% elif trace_error %}`, so removing the `policy_failure is not None`
    short-circuit in the route leaves the page IDENTICAL -- the typed message is
    computed and then never rendered. Measured: with that short-circuit deleted
    the whole suite stays green, so every page-level assertion, including the
    one above, is blind to it.

    What the ordering actually buys is that a request whose alias file already
    failed does not READ a file it does not need. That is observable only at the
    call, so it is asserted at the call.
    """
    from verinote.web import app as web_app

    calls = []
    real = web_app.store_typed_relations

    def spy(store):
        calls.append(1)
        return real(store)

    monkeypatch.setattr(web_app, "store_typed_relations", spy)

    client, _ = _build(tmp_path / "broken_alias", typed=TYPED_BROKEN,
                       alias=ALIAS_CP949)
    assert client.get("/report").status_code == 200
    assert calls == [], "the typed file was read although the alias file failed"

    # Anti-vacuity: the spy DOES fire when the alias file is healthy, so the
    # assertion above is not passing because the patch missed its target.
    calls.clear()
    healthy, _ = _build(tmp_path / "healthy_alias", typed=TYPED_BROKEN)
    assert healthy.get("/report").status_code == 200
    assert calls, "the spy never fired -- it is patched at the wrong name"


def test_a_healthy_pair_renders_the_trace(tmp_path):
    """Anti-vacuity for every assertion above: a guard that fired unconditionally
    would satisfy them all. The traced answer exists only because both files are
    healthy."""
    client, _ = _build(tmp_path)
    body = _flat(client)
    assert PAGE_WITHHELD not in body
    assert TRACE_WITHHELD not in body
    assert NOT_COMPUTED not in body
    assert "Contributing facts" in body
    assert "sources/a.txt" in body


@pytest.mark.parametrize(
    "route", ["/questions", "/", "/workbench", "/review", "/facts/1/row",
              "/facts/1/provenance"]
)
def test_no_other_route_is_disturbed_by_a_broken_typed_file(tmp_path, route):
    """AC-5, at the routes rather than by reading. `fact_trust_summary` reads
    both policy files and is reached from five places package-wide; every one
    other than `/report` is already covered upstream -- #585/#570's
    `_trust_policy_failure` for the fact-row family and the dashboard, #591's
    snapshot guard for `POST /ask`. Spied on this tree: on a broken typed file
    these make ZERO calls to it and stay 200."""
    client, _ = _build(tmp_path, typed=TYPED_BROKEN)
    assert client.get(route).status_code == 200
