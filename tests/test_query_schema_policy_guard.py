# SPDX-License-Identifier: MPL-2.0
r"""A broken trust-policy file degrades the query-schema path instead of raising
out of it (#591).

`build_query_schema_snapshot` reads `policy/relation-aliases.md` and
`policy/typed-relations.md` in its first two statements. Three entry points share
that statement -- `POST /ask`, `POST /questions/translate` and `verinote query`
-- which is why the guard is `query_schema_policy_failure` in the pipeline and
not another `web/app.py` nested function: a web guard cannot reach the CLI.

THREE BROKEN INPUTS, NOT TWO. #591's repro section names only cp949 for the
alias file. Measured on `b4dea1b`, a MALFORMED alias file 500s these routes as
well: this call site has no narrow `except CorroborationPolicyError` in front of
it, unlike `verify.py`, so it does not survive the malformed case the way
`GET /questions` and `GET /report` do.

`TYPED_TYPO` BECAME A BROKEN INPUT IN #589, and this paragraph used to say the
opposite. #585 measured that `typed_relations` silently skipped lines it could
not parse, so a typo'd typed file parsed to `{}` and failed nothing. #589 made
that line raise, so the guard now fires on it and `POST /ask` names the typed
file in its body.

The STATUS codes did not move -- `POST /ask` is still 200 and
`POST /questions/translate` still 303, because this guard degrades rather than
500s -- which is why the test below kept passing after the behaviour inverted:
`ALL_MESSAGES` had been enumerated when the only typed-file message was the
duplicate-alias one, so the unparseable-line message #589 added was not in it.
It IS in it now -- the set gained `TYPED_TYPO_MSG` in the same change that
found the gap, and both consuming loops picked it up without being touched.
A message set that lists the conditions known at the time silently stops
covering a condition added later; the test below asserts the message that IS
produced, rather than that no known message appears.

Promoting `TYPED_TYPO` into `BROKEN_INPUTS` would now be correct and would give
every parametrized test above a fourth real input. It is deliberately NOT done
here, because that is coverage this issue did not set out to add and belongs to
whoever next works this file.

THE EMPTY-QUEUE TRAP. `POST /questions/translate` returns 303 when there are no
pending questions, under a broken policy file just as under a healthy one.
A fixture without a pending question records a clean cell that is not clean, so
every fixture here carries one and `test_the_fixture_really_has_a_translatable_question`
pins that the queue is doing work.

THE VACUITY TRAP ON THE CLI, WHICH IS THE ONE THAT BITES. A policy failure and a
missing API key produce the SAME status (`translation_failed`) and the SAME exit
code (1). Measured on `b4dea1b` with no credentials: an `UNKNOWN_OR_UNSUPPORTED`
question exits 1 with `translation_failed`, and so does every broken policy file.
So a CLI test asserting `rc != 0`, or even asserting the status, passes
identically with and without this guard. Every CLI test below therefore asserts
the POLICY FILE'S OWN MESSAGE, and ships with a healthy-file control on the same
stub asserting that message is ABSENT -- so the assertion is shown to
discriminate rather than merely to match.

WHAT THE EXIT CODE DOES AND DOES NOT DO. On each broken file `verinote query`
exited 1 before this change and exits 1 after it. The number does not move; what
produces it does, from an unhandled traceback to a reported failure. The healthy
exit code is a property of the FIXTURE, not of the environment: this fixture's
question resolves deterministically and exits 0 with no credentials at all,
which `NoProviderClient` below makes self-enforcing by raising if anything asks
the provider.

WHAT THIS CHANGE DOES NOT DELIVER, stated so its absence is not read as an
oversight. `POST /questions/translate` answers 303 and redirects to
`GET /questions`, and the guard deliberately writes nothing to the question rows
(see below), so that page has nothing to display about the failure. Rendering
the page from the translate route instead of redirecting would have turned the
cp949 input from 303 into 500, because `GET /questions` itself failed on a cp949
alias file when this was written. **#590 has since fixed that** -- `GET /questions`
answers 200 on both broken-alias entries in `BROKEN_INPUTS` below
(`alias-malformed` and `alias-cp949`; the third has a healthy alias) -- so the
500 half of this obstacle is gone. What remains is the other half: this guard
deliberately writes nothing to the question rows, so the landing page still has
no per-question record to render. Delivering the diagnosis needs the message
carried from the POST to the render, which is neither issue's.

WHY THE QUESTION ROWS STAY `pending`. `translate_questions` reports the policy
failure per question but does not write it. #591's justification was comparative
and deliberately narrow: today's unhandled exception leaves every pending
question `pending`, so a guard that wrote `translation_failed` to each would
leave the KB in a worse state than the bug it replaces.

#592 REPLACED THAT WITH A RULE, and this paragraph used to say there was none --
that `_fail_pending_translations` and `cli.py`'s credential path recorded such
faults deliberately and were left alone. Both have since stopped: an
infrastructure fault is REPORTED, never RECORDED, at all four writers. The rule
is read off `translation_failed`'s own definition, "The provider output could
not be used", which is false when no output existed. A response that ARRIVED and
was unusable is a different case and is still recorded.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import verinote.cli as cli
import verinote.llm as llm
import verinote.pipeline.query as query_module
from verinote.pipeline.query import translate_questions
import verinote.pipeline.query_schema as query_schema_module
from verinote.config import Config
from verinote.policy_defaults import RELATION_ALIASES_RELPATH, TYPED_RELATIONS_RELPATH
from verinote.store import Store
from verinote.web import create_app

# The alias parser raises on the FIRST line it cannot parse, so a missing arrow
# is a real failure here. Since #589 the typed parser has the same rule, so this
# input is a real failure in BOTH files -- it used to be a real failure only in
# this one.
ALIAS_MALFORMED = "- 소속 member_of\n".encode()
ALIAS_CP949 = "- 소속 -> member_of\n".encode("cp949")
ALIAS_HEALTHY = "- 소속 -> member_of\n".encode()
# A duplicate alias: one of the conditions `typed_relations` raises on. They are
# enumerated in `test_typed_relations_web_guard.py`'s module docstring and
# DERIVED from the source by
# `test_the_raise_conditions_are_derived_from_the_source_not_listed_by_hand`,
# which reddens if that list stops matching the parser. Cited rather than
# counted or copied here: this comment said "four" and an earlier repair said
# the parser's own docstring enumerated them, and all three were wrong -- the
# count twice, and the pointer at a docstring that lists no condition at all.
TYPED_DUP_ALIAS = "- 자본금: amount as capital\n- 자산: amount as capital\n".encode()
TYPED_HEALTHY = "- 자본금: amount as capital\n".encode()
TYPED_TYPO = "- 소속 member_of\n".encode()

ALIAS_PARSER_MSG = "relation-aliases.md:"
ALIAS_NAMED = f"{RELATION_ALIASES_RELPATH} could not be read"
TYPED_PARSER_MSG = "typed-relations.md: alias"
# #589's unparseable-line message. A DIFFERENT string from the one above, which
# is exactly why `ALL_MESSAGES` did not catch it.
TYPED_TYPO_MSG = "typed-relations.md:1: expected"

# (alias bytes, typed bytes, the message that must appear). Three inputs.
BROKEN_INPUTS = [
    pytest.param(ALIAS_MALFORMED, TYPED_HEALTHY, ALIAS_PARSER_MSG, id="alias-malformed"),
    pytest.param(ALIAS_CP949, TYPED_HEALTHY, ALIAS_NAMED, id="alias-cp949"),
    pytest.param(ALIAS_HEALTHY, TYPED_DUP_ALIAS, TYPED_PARSER_MSG, id="typed-duplicate-alias"),
]
# Every policy-file message a guarded route can render. `TYPED_TYPO_MSG` is a
# member because #589 made the typed parser strict: a false-positive parse error
# on a VALID file is newly possible, and the healthy-KB controls that consume
# this tuple are what would catch it. Add to this tuple, never to its consumers
# -- both surviving loops read it, and the previous member added to one of them
# instead is why a test kept passing after its behaviour inverted.
ALL_MESSAGES = (ALIAS_PARSER_MSG, ALIAS_NAMED, TYPED_PARSER_MSG, TYPED_TYPO_MSG)

# Resolved by `deterministic_query_intent`, so the provider is never asked and
# the healthy exit code is 0 without credentials. `NoProviderClient` enforces it.
QUESTION = "A의 자본금은?"


class NoProviderClient:
    """An `LLMClient` that raises if anything asks the provider.

    Not decoration: it is what makes "the provider was never reached" an
    assertion rather than an assumption, and it is what keeps the CLI tests
    below from silently measuring the missing-API-key path, which produces the
    same status and the same exit code as a policy failure.
    """

    name = "no-provider"

    def extract_query_intent(self, **kwargs):
        raise AssertionError("the provider was asked: extract_query_intent")

    def translate_query(self, **kwargs):
        raise AssertionError("the provider was asked: translate_query")

    def answer_question(self, **kwargs):
        raise AssertionError("the provider was asked: answer_question")

    def extract_facts(self, **kwargs):
        raise AssertionError("the provider was asked: extract_facts")


def _seed(root: Path, alias_bytes: bytes, typed_bytes: bytes) -> Config:
    cfg = Config(
        root=root,
        db_path=root / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    store = Store(cfg.db_path)
    store.init_schema()
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "a.txt").write_text("x\n", encoding="utf-8")
    source_id = store.add_source("sources/a.txt")
    store.add_fact(
        "A", "자본금", "1억", status="confirmed", confidence=0.9, source_id=source_id
    )
    store.add_question(QUESTION)
    store.close()
    for relpath, data in (
        (RELATION_ALIASES_RELPATH, alias_bytes),
        (TYPED_RELATIONS_RELPATH, typed_bytes),
    ):
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return cfg


def _client(tmp_path: Path, alias_bytes: bytes, typed_bytes: bytes):
    root = tmp_path / "kb"
    root.mkdir()
    cfg = _seed(root, alias_bytes, typed_bytes)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False), app


ASK_RESULT_RE = re.compile(r'<section class="ask-result.*?</section>', re.S)


# ---------------------------------------------------------------------------
# POST /ask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_ask_survives_each_broken_policy_file(tmp_path, alias_bytes, typed_bytes, message):
    """Each of these answered 500 on the parent commit. The assertion is the
    MESSAGE and not the status, for #590's reason: the guard's two clauses both
    return a string, so deleting the narrow one leaves the status unchanged and
    only swaps the bare parser message for a "could not be read" wrapper."""
    client, _ = _client(tmp_path, alias_bytes, typed_bytes)
    r = client.post("/ask", data={"question": QUESTION})
    assert r.status_code == 200
    assert message in r.text


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_ask_carries_the_diagnosis_in_the_answer_not_only_on_the_page(
    tmp_path, alias_bytes, typed_bytes, message
):
    """`GET /ask` renders clean and stays clean -- it has no page-level banner
    to fall back on -- so the `AskResult` is the only place a user learns
    anything. Asserted inside the `ask-result` section rather than anywhere in
    the body, so a diagnosis printed somewhere else would not satisfy it."""
    client, _ = _client(tmp_path, alias_bytes, typed_bytes)
    section = ASK_RESULT_RE.search(client.post("/ask", data={"question": QUESTION}).text)
    assert section is not None, "the page rendered no ask-result section at all"
    assert message in section.group(0)


def test_a_healthy_policy_pair_still_answers_and_says_nothing_about_policy(tmp_path):
    """Anti-vacuity control: a guard that fired unconditionally would put a
    diagnosis on every healthy KB too."""
    client, _ = _client(tmp_path, ALIAS_HEALTHY, TYPED_HEALTHY)
    r = client.post("/ask", data={"question": QUESTION})
    assert r.status_code == 200
    for message in ALL_MESSAGES:
        assert message not in r.text


def test_a_typod_typed_file_is_reported_since_589(tmp_path):
    """INVERTED BY #589, which is why the assertion is on the MESSAGE.

    This asserted that no member of `ALL_MESSAGES` appeared, and it kept passing
    that way after the behaviour inverted: the set had been enumerated when the
    only typed-file message was the duplicate-alias one, so the unparseable-line
    message #589 added slipped straight through it, and the status is 200 either
    way. Nothing about the old form could fail.

    Both halves have since been fixed and they are independent. The set gained
    `TYPED_TYPO_MSG`, so the old form would now FAIL here -- measured, that
    message is in the body. And this test no longer uses the old form: asserting
    the message that IS produced is what tells the two eras apart regardless of
    what the set happens to contain.
    """
    client, _ = _client(tmp_path, ALIAS_HEALTHY, TYPED_TYPO)
    r = client.post("/ask", data={"question": QUESTION})
    assert r.status_code == 200
    assert TYPED_TYPO_MSG in r.text
    # Still not the OTHER file's failure, and still not a read failure.
    assert ALIAS_PARSER_MSG not in r.text
    assert ALIAS_NAMED not in r.text


# ---------------------------------------------------------------------------
# POST /questions/translate, and the rows it must not write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_translate_survives_each_broken_policy_file(
    tmp_path, alias_bytes, typed_bytes, message
):
    """The POST itself, not the redirect target.

    When this was written the reason was that `GET /questions` 500ed on a cp949
    alias file, so following the redirect measured that route instead of this
    one. #590 fixed that and the reason changed rather than disappearing: the
    redirect target is a different route with its own guard and its own tests,
    so asserting on it here would attribute #590's behaviour to #591's guard.
    THE STATUS CHANGED IN #592, and the reason for asserting here did not. This
    asserted 303: the route redirected and the diagnosis lived in the question
    rows it wrote. #592 stopped writing those rows -- an infrastructure fault is
    reported, never recorded -- so the route now RENDERS the page with the fault
    in its error slot, which is a 200. The message is asserted rather than the
    bare status, because the status alone no longer distinguishes "reported" from
    "silently redirected".
    """
    client, _ = _client(tmp_path, alias_bytes, typed_bytes)
    r = client.post("/questions/translate", follow_redirects=False)
    assert r.status_code == 200
    body = " ".join(r.text.split())
    assert "Translation did not run" in body
    assert message in body


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_translate_leaves_every_question_pending(
    tmp_path, alias_bytes, typed_bytes, message
):
    """The write suppression. Without it one unreadable policy file marks every
    pending question `translation_failed` -- a durable, audited claim that each
    question failed translation, when translation was never attempted. Today's
    crash writes nothing, so writing here would leave the KB worse than the bug.
    """
    del message
    client, app = _client(tmp_path, alias_bytes, typed_bytes)
    client.post("/questions/translate", follow_redirects=False)
    assert [q["status"] for q in app.state.store.questions()] == ["pending"]


def test_a_file_that_breaks_mid_run_still_writes_no_row(tmp_path):
    """The regression an earlier revision of this guard shipped, pinned.

    That revision read the policy files ONCE before the loop and suppressed the
    write on that reading, while the flow re-read them per question. With the
    files healthy at the pre-loop read and broken before the first question's
    flow ran, the two readings disagreed: the fresh one put `translation_failed`
    and the policy message into the row's `status` and `reason`, and the stale
    one let the write through. Measured against the parent commit, which raised
    and wrote nothing, that was strictly worse than the bug the guard replaces
    -- it is the exact false record the suppression exists to prevent.

    The corruption is injected from `store.questions()` so that it lands after
    any pre-loop read and before the first flow call, which is the only window
    where the two readings can differ. A build that reintroduces a separate
    pre-loop verdict fails here and nowhere else in this file.
    """
    root = tmp_path / "kb"
    root.mkdir()
    cfg = _seed(root, ALIAS_HEALTHY, TYPED_HEALTHY)
    store = Store(cfg.db_path)
    alias_path = root / RELATION_ALIASES_RELPATH
    real_questions = store.questions

    def questions_then_corrupt(*args, **kwargs):
        rows = real_questions(*args, **kwargs)
        alias_path.write_bytes(ALIAS_MALFORMED)
        return rows

    store.questions = questions_then_corrupt
    try:
        results = translate_questions(store, NoProviderClient(), root=root)
    finally:
        store.questions = real_questions
    # The run reports the failure ...
    assert results and all(r["status"] == "translation_failed" for r in results)
    assert all(ALIAS_PARSER_MSG in r["reason"] for r in results)
    # ... and writes none of it to the rows.
    assert [q["status"] for q in store.questions()] == ["pending"]
    store.close()


def test_the_fixture_really_has_a_translatable_question(tmp_path):
    """The empty-queue trap. `POST /questions/translate` answers 303 with no
    pending questions, broken file or not, so the two tests above would pass on
    an empty queue while proving nothing. On a healthy pair the same fixture
    really does translate."""
    client, app = _client(tmp_path, ALIAS_HEALTHY, TYPED_HEALTHY)
    assert [q["status"] for q in app.state.store.questions()] == ["pending"]
    client.post("/questions/translate", follow_redirects=False)
    assert [q["status"] for q in app.state.store.questions()] == ["translated"]


# ---------------------------------------------------------------------------
# The status the guard returns is load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_repair_does_not_reach_the_direct_datalog_fallback(
    tmp_path, alias_bytes, typed_bytes, message, monkeypatch
):
    """`translation_failed` rather than `review_required`, instrumented rather
    than inferred. `repair.py` falls through to `_translate_direct_datalog_fallback`
    exactly on `review_required`, which calls `build_query_schema_snapshot`
    again and would re-raise the failure the guard just caught. Counting the
    call is the only way to see that; a status code cannot."""
    del message
    from verinote.pipeline import repair as repair_module

    root = tmp_path / "kb"
    root.mkdir()
    cfg = _seed(root, alias_bytes, typed_bytes)
    calls = []
    monkeypatch.setattr(
        repair_module,
        "_translate_direct_datalog_fallback",
        lambda *a, **k: calls.append(1),
    )
    store = Store(cfg.db_path)
    try:
        prepared = repair_module._prepare_repair_question(
            store,
            NoProviderClient(),
            question_id=1,
            question=QUESTION,
            previous_query_dl=None,
        )
    finally:
        store.close()
    assert calls == []
    assert prepared.status == "translation_failed"


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_the_guard_stops_the_snapshot_from_being_built_at_all(
    tmp_path, alias_bytes, typed_bytes, message, monkeypatch
):
    """Reachability, measured at the guarded function itself. Every probe routed
    through `build_query_schema_snapshot` inherits the guard, so the count has to
    come from that function -- inferring it from a status code proves nothing."""
    del message
    calls = []
    real = query_schema_module.build_query_schema_snapshot

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(query_schema_module, "build_query_schema_snapshot", counting)
    monkeypatch.setattr(query_module, "build_query_schema_snapshot", counting)

    client, _ = _client(tmp_path, alias_bytes, typed_bytes)
    client.post("/ask", data={"question": QUESTION})
    client.post("/questions/translate", follow_redirects=False)
    assert calls == []


def test_a_healthy_policy_pair_does_build_the_snapshot(tmp_path, monkeypatch):
    """Anti-vacuity control for the count above: zero is only meaningful if the
    same instrument reads non-zero when the guard does not trip."""
    calls = []
    real = query_schema_module.build_query_schema_snapshot

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(query_schema_module, "build_query_schema_snapshot", counting)
    monkeypatch.setattr(query_module, "build_query_schema_snapshot", counting)

    client, _ = _client(tmp_path, ALIAS_HEALTHY, TYPED_HEALTHY)
    client.post("/ask", data={"question": QUESTION})
    assert calls


# ---------------------------------------------------------------------------
# verinote query
# ---------------------------------------------------------------------------


def _run_cli_query(tmp_path: Path, alias_bytes: bytes, typed_bytes: bytes, monkeypatch):
    """`cmd_query` in process, with the provider stubbed to raise if reached.

    In process rather than as a subprocess on purpose: the thing to assert is
    that the command RETURNS an exit code instead of raising, which is the
    "no traceback" half stated as a property rather than as string-matching on
    stderr. `cmd_query` imports `get_client` inside the function, so patching
    `verinote.llm.get_client` reaches it.
    """
    root = tmp_path / "kb"
    root.mkdir()
    cfg = _seed(root, alias_bytes, typed_bytes)
    monkeypatch.setattr(llm, "get_client", lambda cfg: NoProviderClient())
    rc = cli.cmd_query(cfg, SimpleNamespace(question=None))
    store = Store(cfg.db_path)
    statuses = [q["status"] for q in store.questions()]
    store.close()
    return rc, statuses


@pytest.mark.parametrize("alias_bytes, typed_bytes, message", BROKEN_INPUTS)
def test_verinote_query_reports_the_policy_failure_instead_of_crashing(
    tmp_path, alias_bytes, typed_bytes, message, monkeypatch, capsys
):
    """`rc=1` before this change and `rc=1` after it -- the number does not move,
    and this test does not pretend it does. What moves is that `cmd_query`
    returns rather than raising, and that the file's own message is printed."""
    rc, statuses = _run_cli_query(tmp_path, alias_bytes, typed_bytes, monkeypatch)
    assert rc == 1
    assert message in capsys.readouterr().out
    assert statuses == ["pending"]


def test_a_healthy_policy_pair_produces_no_policy_message_on_the_cli(
    tmp_path, monkeypatch, capsys
):
    """The control that makes the assertion above discriminate. Without it,
    `rc == 1` plus `translation_failed` is exactly what a missing API key
    produces, and the test would match that instead of the guard."""
    rc, statuses = _run_cli_query(tmp_path, ALIAS_HEALTHY, TYPED_HEALTHY, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    for message in ALL_MESSAGES:
        assert message not in out
    assert statuses == ["translated"]
