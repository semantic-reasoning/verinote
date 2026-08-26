# SPDX-License-Identifier: MPL-2.0
"""#592. An infrastructure fault is REPORTED, never RECORDED on a question row.

THE RULE IS ANCHORED IN THE STATUS, not chosen -- anchored rather than READ OFF,
because `question_outcome.py::_STATUS_META`'s "The provider output could not be
used" is a display FALLBACK that `question_outcome_view` renders only for a row
carrying no reason. It is still the only textual statement of what
`translation_failed` means anywhere in this repo, and it is a claim about the
provider's OUTPUT. When no request was sent (no API key, unknown
provider, SDK missing) or translation was never attempted (a policy file that
cannot be read), there is no output and the claim is false. `pending` ("Waiting
for translation.") is true of all of them, and it is already in `db.py`'s CHECK
constraint, so nothing here needs a schema change.

WHAT IS NOT AN INFRASTRUCTURE FAULT, and this is the half that makes the rule
non-vacuous: a response that ARRIVED and could not be used. A schema-violating
intent, a payload with no tool_use block, an undecodable CLI answer -- the
provider was reached and misbehaved, `translation_failed` is exactly true of the
row, and it IS recorded. Without that half the rule would be satisfiable by
never recording anything and the status would become unreachable.

`LLMError` could not express that distinction: its own docstring is "Any
provider-side OR PARSING failure", so it conflates a request never sent with an
answer that came back unusable. `LLMOutputError` is the second case, and the
tests below pin both directions.
"""
import ast
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import verinote.web.app as webapp
from verinote.config import Config
from verinote.llm.base import LLMError, LLMOutputError, parsed_under_redaction
from verinote.llm.schema import parse_facts, parse_query
from verinote.pipeline.query import translate_questions
from verinote.pipeline.query_intent import parse_query_intent
from verinote.pipeline.repair import process_repair_job, repair_question
from verinote.store import Store
from verinote.web.app import create_app

ALIAS_HEALTHY = "- 소속 -> member_of\n"
TYPED_HEALTHY = "- 자본금: amount as capital\n"
TYPED_BROKEN = "- 자본금: amount as capital\n- 자산: amount as capital\n"


def _kb(root: Path, *, typed: str = TYPED_HEALTHY) -> Store:
    root.mkdir(parents=True, exist_ok=True)
    policy = root / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "relation-aliases.md").write_text(ALIAS_HEALTHY, encoding="utf-8")
    (policy / "typed-relations.md").write_text(typed, encoding="utf-8")
    store = Store(root / "kb.sqlite")
    store.init_schema()
    store.add_question("What is the sample answer?")
    return store


def _statuses(store: Store) -> list[str]:
    return [str(q["status"]) for q in store.questions()]


class _NeverReached:
    """The provider is never reached: the adapter fails before sending."""

    def extract_query_intent(self, *, question, schema_hint=""):
        raise LLMError("anthropic requires an API key; set VERINOTE_ANTHROPIC_API_KEY")

    def translate_query(self, *, question, qid, schema_hint=""):
        raise LLMError("anthropic requires an API key; set VERINOTE_ANTHROPIC_API_KEY")


class _AnswersUnusably:
    """The provider ANSWERS, and the answer cannot be used.

    Built through the real parser rather than by raising `LLMOutputError`
    directly: a stub that raised the class under test would pass whatever the
    production code did with it, which is the vacuity this file exists to avoid.
    """

    def extract_query_intent(self, *, question, schema_hint=""):
        return parse_query_intent({"kind": "lookup_object"})

    def translate_query(self, *, question, qid, schema_hint=""):
        # The repair path falls through to this after the intent comes back
        # unsupported. It ANSWERS -- with Datalog the engine cannot use.
        return "answer_q1(X) :- nonexistent_predicate(X)."


UNUSABLE_QUESTION = "Which unusable answer is recorded?"
UNREACHED_QUESTION = "What is the sample answer?"


class _AnswersOneAndNeverReachesTheOther:
    """One client, both sides of the discriminator, keyed on the question text.

    `translate_questions` uses ONE client for every question in a run, so the
    only way to reach a run where one row is written and another deliberately is
    not -- the single state in which the status string and the verdict disagree
    -- is a client that fails differently per question.
    """

    _UNREACHED = "anthropic requires an API key; set VERINOTE_ANTHROPIC_API_KEY"

    def extract_query_intent(self, *, question, schema_hint=""):
        if question == UNUSABLE_QUESTION:
            return parse_query_intent({"kind": "lookup_object"})
        raise LLMError(self._UNREACHED)

    def translate_query(self, *, question, qid, schema_hint=""):
        if question == UNUSABLE_QUESTION:
            return "answer_q1(X) :- nonexistent_predicate(X)."
        raise LLMError(self._UNREACHED)


def _app_with(root: Path, client_obj, monkeypatch, *, questions):
    root.mkdir(parents=True, exist_ok=True)
    policy = root / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "relation-aliases.md").write_text(ALIAS_HEALTHY, encoding="utf-8")
    (policy / "typed-relations.md").write_text(TYPED_HEALTHY, encoding="utf-8")
    cfg = Config(root=root, db_path=root / "kb.sqlite", provider="anthropic",
                 model="m", api_key=None, base_url=None)
    monkeypatch.setattr(webapp, "get_client", lambda cfg: client_obj)
    app = create_app(cfg)
    for text in questions:
        app.state.store.add_question(text)
    return app


_BANNER_RE = re.compile(r'<p class="error" role="alert">(.*?)</p>', re.S)


def _banner(response) -> str | None:
    """The banner ALONE, not the page.

    The page also renders every question row, so a substring search over the
    whole body cannot tell a reason that reached the banner from the same reason
    reaching the row that owns it -- which is exactly the confusion this surface
    was blocked for.
    """
    match = _BANNER_RE.search(response.text)
    return None if match is None else " ".join(unescape(match.group(1)).split())


def test_the_unusable_output_fixture_really_raises_through_the_real_parser():
    """Anti-vacuity for `_AnswersUnusably`, and for the rule's second half.

    If `parse_query_intent` ever stopped rejecting this payload, every
    "still recorded" test below would pass by never failing at all.
    """
    with pytest.raises(LLMOutputError) as excinfo:
        parse_query_intent({"kind": "lookup_object"})
    assert "did not match schema" in str(excinfo.value)


def test_a_provider_that_was_never_reached_is_not_recorded(tmp_path):
    store = _kb(tmp_path / "kb")
    results = translate_questions(store, _NeverReached(), root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]
    # Reported, though: the caller still receives it, which is what the CLI's
    # exit code and the web's banner are both derived from.
    assert results[0]["status"] == "translation_failed"
    assert "API key" in results[0]["reason"]


def test_an_unreadable_policy_file_is_not_recorded(tmp_path):
    store = _kb(tmp_path / "kb", typed=TYPED_BROKEN)
    results = translate_questions(store, _NeverReached(), root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]
    assert results[0]["status"] == "translation_failed"


def test_output_that_arrived_and_could_not_be_used_IS_recorded(tmp_path):
    """THE ANTI-VACUITY CONTROL. Without this the rule is satisfiable by never
    recording anything, and `translation_failed` becomes unreachable."""
    store = _kb(tmp_path / "kb")
    translate_questions(store, _AnswersUnusably(), root=tmp_path / "kb")
    assert _statuses(store) == ["translation_failed"]
    assert "did not match schema" in str(store.questions()[0]["reason"])


def test_repair_does_not_record_an_unreadable_policy_file(tmp_path):
    """The SECOND writer. `repair_question` persists `prepared.status`, which a
    policy fault reaches as `translation_failed`."""
    root = tmp_path / "kb"
    store = _kb(root, typed=TYPED_BROKEN)
    qid = int(store.questions()[0]["id"])
    repair_question(store, _NeverReached(), question_id=qid,
                    question="What is the sample answer?", root=root)
    assert _statuses(store) == ["pending"]


def test_the_web_reports_the_fault_it_no_longer_records(tmp_path):
    """AC-2's cost, and the half of it that is NOT paid.

    The row write WAS the web's only diagnosis, so deleting it without this
    surface would have made a broken provider silent. What the banner does not
    replace is DURABILITY, measured with one fixture and only the tree varying:
    on `6cd66b8` the row carried the reason and every later `GET /questions`
    rendered it, while here the banner lives in the POST response alone and the
    next GET shows "Waiting for translation." and nothing else. Nothing in the
    database, the KB directory or the log holds the fault. With a repair job
    pending it is shorter still: that page polls `GET /questions` every two
    seconds and swaps the body, so the first tick replaces the banner.

    The trade is defensible -- a transient true report beats a durable false one,
    which is the whole of #592 -- but it is a trade and not a free move. The
    other two surfaces kept a durable home for the same class of fault: the CLI
    has stdout, stderr and rc=1, and web repair persists `repair_jobs.message`,
    which survives a reload. Translate is the one surface with no durable
    equivalent, so this is an outlier rather than the pattern.
    """
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    policy = root / "policy"
    policy.mkdir(parents=True)
    (policy / "relation-aliases.md").write_text(ALIAS_HEALTHY, encoding="utf-8")
    (policy / "typed-relations.md").write_text(TYPED_HEALTHY, encoding="utf-8")
    cfg = Config(root=root, db_path=root / "kb.sqlite", provider="nosuchprovider",
                 model="m", api_key=None, base_url=None)
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    app.state.store.add_question("What is the sample answer?")

    response = client.post("/questions/translate", follow_redirects=False)

    assert response.status_code == 200
    assert "Translation could not run" in " ".join(response.text.split())
    assert [str(q["status"]) for q in app.state.store.questions()] == ["pending"]


def test_the_web_says_nothing_about_a_fault_it_recorded(tmp_path, monkeypatch):
    """The banner reports what the ROW could not, so a written row has nothing
    for it to report.

    Filtering the route on `r["status"] == "translation_failed"` instead of on
    the verdict makes this run render "Translation could not run" over a row the
    same request had just marked `Translation failed`. The redirect is the
    assertion: it is reachable only when the route finds no suppressed fault.
    """
    root = tmp_path / "kb"
    app = _app_with(root, _AnswersUnusably(), monkeypatch, questions=[UNUSABLE_QUESTION])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/questions/translate", follow_redirects=False)

    assert response.status_code == 303
    row = app.state.store.questions()[0]
    assert str(row["status"]) == "translation_failed"
    assert "did not match schema" in str(row["reason"])


def test_a_mixed_run_reports_the_suppressed_fault_and_not_the_recorded_one(
    tmp_path, monkeypatch
):
    """The state that separates the two filters, and the reason the verdict has
    to travel on the result dict.

    Both dicts say `translation_failed`: the recorded one because that is what
    its row says, the suppressed one because `cmd_query` derives its exit code
    from these same dicts. A route filtering on the status string takes
    `faults[0]` -- the RECORDED question, ordered first here on purpose, since
    `Store.questions()` is `ORDER BY id` -- and speaks for a row it did not
    suppress, while the genuine infrastructure fault on the second question goes
    unmentioned. Filtering on `infrastructure_fault` takes the second.
    """
    root = tmp_path / "kb"
    app = _app_with(root, _AnswersOneAndNeverReachesTheOther(), monkeypatch,
                    questions=[UNUSABLE_QUESTION, UNREACHED_QUESTION])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/questions/translate", follow_redirects=False)

    assert response.status_code == 200
    rows = {str(q["text"]): str(q["status"]) for q in app.state.store.questions()}
    assert rows == {UNUSABLE_QUESTION: "translation_failed",
                    UNREACHED_QUESTION: "pending"}
    banner = _banner(response)
    assert banner is not None
    assert "API key" in banner
    # The recorded question's reason belongs on its ROW, which this same page
    # renders. What must not happen is the banner speaking for it.
    assert "did not match schema" not in banner


def test_no_shipped_llm_error_subclass_defines_its_own_str():
    """The RELABEL's precondition, enforced -- the hazard the deleted tripwire
    did not cover and its replacement could not.

    `parsed_under_redaction` redacts by rewriting `exc.args` and re-raising the
    same object. That reaches `str(exc)` only because `BaseException.__str__`
    derives the string from `args`. A subclass that defines `__str__` -- to cache
    its message, or to format one from other fields -- keeps returning the
    ORIGINAL text after the rewrite, so the secret survives into a message this
    function exists to redact. Measured on this primitive: such a subclass leaks
    where the previous revision's rebuild did not. It is a fail-OPEN direction in
    the one place whose whole job is redaction.

    The tripwire this replaces checked constructor ARITY, which was the REBUILD's
    hazard and is now structurally impossible. It would not have caught this one.

    DERIVED FROM THE SOURCE, not from `__subclasses__()`, and that is the point:
    `__subclasses__()` returns direct children only and sees nothing a test run
    has not imported, so the old sweep was blind exactly where a new subclass
    would be added. This walks every class statement under `verinote/`, resolves
    the `LLMError` family as a fixpoint over base names, and so covers a subclass
    at any depth in any module whether or not anything imports it.

    SCOPE, so it is not read as more than it is: this cannot see a subclass
    defined outside `verinote/`, and it checks `__str__` rather than every way a
    message could be decoupled from `args`. `__str__` is the decisive one --
    without it, `str(exc)` reads `args` and the redaction holds.
    """
    package = Path(__file__).resolve().parent.parent / "verinote"
    classes = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                bases |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
                classes[node.name] = (bases, node, path)

    family = {"LLMError"}
    while True:
        grown = {
            name for name, (bases, _, _) in classes.items() if bases & family
        } | family
        if grown == family:
            break
        family = grown

    swept = sorted(family - {"LLMError"})
    assert swept, (
        "no LLMError subclass was found under verinote/, so this sweep judged "
        "nothing -- it has been made vacuous, not satisfied"
    )

    offenders = []
    for name in swept:
        _, node, path = classes[name]
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name == "__str__":
                offenders.append(f"{path.name}:{child.lineno} {name}.__str__")

    assert offenders == [], (
        "these `LLMError` subclasses define `__str__`, so `parsed_under_redaction`"
        " cannot redact them: it rewrites `args` and re-raises the same object, "
        f"and their `__str__` keeps returning the unredacted text. {offenders}"
    )


def test_parsed_under_redaction_preserves_a_deep_subclass_and_its_state():
    """What replaced the subclass tripwire, and it pins the property directly.

    The primitive used to rebuild the exception as `type(exc)(message)`, which
    imposed a constructor contract on every `LLMError` subclass. That contract
    was guarded by a test walking `LLMError.__subclasses__()` -- a check that
    could not see a subclass one level deeper, could not see one in a module
    nothing had imported yet, and would have passed a subclass that took one
    message and silently dropped everything else it carried.

    The primitive relabels the exception in place now, so there is no contract
    left to guard and the guard is gone with it. This asserts the property the
    guard was standing in for, on a subclass `__subclasses__()` could never have
    reached: two constructor arguments, one level below `LLMOutputError`. Both
    halves matter -- the class and the `status_code` survive, and the secret
    still does not.
    """
    secret = "sk-ant-SECRETVALUE0123456789"

    class _Throttled(LLMOutputError):
        def __init__(self, message, status_code=503):
            super().__init__(message)
            self.status_code = status_code

    assert _Throttled not in LLMError.__subclasses__()

    def parse(_payload):
        raise _Throttled(f"upstream refused {secret}", 429)

    with pytest.raises(_Throttled) as excinfo:
        parsed_under_redaction(parse, "payload", secret)
    assert excinfo.value.status_code == 429
    assert secret not in str(excinfo.value)
    assert str(excinfo.value) == "upstream refused ***"


def test_parsed_under_redaction_preserves_the_class_and_the_redaction():
    """Both halves together: flattening the class silently disabled the rule,
    and losing the redaction would leak a key into a question row."""
    secret = "sk-ant-SECRETVALUE0123456789"
    with pytest.raises(LLMOutputError) as excinfo:
        parsed_under_redaction(parse_query_intent, f"not json {secret}", secret)
    assert secret not in str(excinfo.value)
    assert "query intent output was not JSON" in str(excinfo.value)


def test_the_async_repair_worker_does_not_record_it_either(tmp_path):
    """THE THIRD WRITER, EXERCISED rather than inspected.

    `persist_repair_question` is a SEPARATE write from `repair_question`'s, on
    the job worker's path, and a guard added to one does not cover the other. The
    three are `translate_questions`, `repair_question` and this one -- derived by
    taking the `Store` methods whose SQL sets `questions.status` and then their
    callers under `verinote/`, which is the same derivation the module docstring
    of tests/test_query_schema_policy_guard.py spells out. It
    would be easy to add the check and never run it -- so this drives the real
    worker end to end and asserts the row, not the presence of a guard.

    The row stays `review_required`, which is what it already was and still
    true: `persist_repair_question` only ever transitions rows out of that
    status, so declining to write leaves a status that describes the question
    rather than the environment. The fault is still reported -- the job and its
    item both finish `failed` with the reason.
    """
    root = tmp_path / "kb"
    store = _kb(root, typed=TYPED_BROKEN)
    qid = int(store.questions()[0]["id"])
    store.set_question_query(qid, 'review_required("synthetic")', "review_required")
    job, created = store.enqueue_repair_job(provider="fake", model="m")
    assert created is True

    process_repair_job(store, _NeverReached(), job_id=int(job["id"]), root=root)

    assert _statuses(store) == ["review_required"]
    items = store.repair_job_items(int(job["id"]))
    assert [str(i["status"]) for i in items] == ["failed"]
    assert "typed-relations.md" in str(items[0]["reason"])


def test_the_async_worker_still_records_output_that_arrived_unusable(tmp_path):
    """The other half, on the same path: a provider that ANSWERS unusably must
    still be written. Without this the async guard could suppress everything and
    both tests above would still pass."""
    root = tmp_path / "kb"
    store = _kb(root)
    qid = int(store.questions()[0]["id"])
    store.set_question_query(qid, 'review_required("synthetic")', "review_required")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")

    before = str(store.questions()[0]["reason"])
    process_repair_job(store, _AnswersUnusably(), job_id=int(job["id"]), root=root)

    # The row was WRITTEN: its reason now describes the unusable answer. The
    # status may legitimately stay `review_required` -- the engine's verdict on
    # a bad draft is review, not translation-failure -- so the reason is what
    # distinguishes "written" from "declined to write", and the status alone
    # would make this test pass either way.
    after = str(store.questions()[0]["reason"])
    assert after != before
    # The intent parser rejects the payload, so the flow never reaches the
    # direct-Datalog fallback -- the recorded reason is the schema violation,
    # which is the provider's output being unusable.
    assert "did not match schema" in after


def test_the_async_worker_fails_the_job_on_output_that_arrived_unusable(tmp_path):
    """The ROW and the JOB are two different reports, and #592 must not silence
    the second while it is fixing the first.

    Substituting `infrastructure_fault` for `result.provider_failed` at the job's
    report site NARROWS as well as widens: an answer that arrives unusable sets
    `provider_failed` and clears `infrastructure_fault`, so the item finishes
    `done` over a question that was never repaired -- worse than the behaviour
    this rule replaced, which failed it. The sibling above drives the same run
    and asserts only the row, and stays green under that substitution, which is
    why the item and the job need an assertion of their own.
    """
    root = tmp_path / "kb"
    store = _kb(root)
    qid = int(store.questions()[0]["id"])
    store.set_question_query(qid, 'review_required("synthetic")', "review_required")
    job, _ = store.enqueue_repair_job(provider="fake", model="m")

    process_repair_job(store, _AnswersUnusably(), job_id=int(job["id"]), root=root)

    items = store.repair_job_items(int(job["id"]))
    assert [str(i["status"]) for i in items] == ["failed"]
    saved = store.get_repair_job(int(job["id"]))
    assert str(saved["status"]) == "failed"
    assert "did not match schema" in str(saved["message"])


# Every `raise LLMOutputError` site the question path can reach, one case each.
# The class is the whole mechanism -- `translate_questions` reads it off the
# exception to decide whether the row records the failure -- so a site raising
# the base class instead silently rejoins the suppressed half, with no other
# symptom. Reverting any one of these to `LLMError` reddens exactly its own case.
_ARRIVED_UNUSABLE_PARSES = {
    "parse_query/off-schema": (parse_query, {}, "query translation did not match schema"),
    "parse_query/empty": (parse_query, {"datalog": "   "}, "query translation was empty"),
    "parse_facts/off-schema": (parse_facts, "not json at all", "extractor output did not match schema"),
    "parse_facts/malformed-item": (
        parse_facts,
        {"facts": [{"subject": "Sample Person", "relation": "born_in"}]},
        "malformed fact object",
    ),
    "parse_query_intent/not-json": (parse_query_intent, "not json at all", "query intent output was not JSON"),
    "parse_query_intent/off-schema": (parse_query_intent, {"kind": "lookup_object"}, "did not match schema"),
}


@pytest.mark.parametrize("case", sorted(_ARRIVED_UNUSABLE_PARSES))
def test_every_parser_rejection_is_an_arrived_unusable_answer(case):
    """A parser only ever runs on a payload that ARRIVED, so every exit it has
    is the recorded half of the rule by construction."""
    parse, payload, fragment = _ARRIVED_UNUSABLE_PARSES[case]
    with pytest.raises(LLMOutputError) as excinfo:
        parse(payload)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "method",
    ["extract_facts", "translate_query", "extract_query_intent"],
)
def test_anthropic_reports_a_missing_tool_use_block_as_unusable_output(
    tmp_path, monkeypatch, method
):
    """The tree's own advertised example, which nothing tested.

    `LLMOutputError`'s docstring offers "a payload with no tool_use block" as the
    case it exists for, and this adapter raises it at three separate exits -- one
    per method, none of them shared -- so one test cannot stand for the others.
    A response with content but no tool_use block ARRIVED: the provider was
    reached and misbehaved, which is what the row records.
    """
    from types import SimpleNamespace

    from verinote.llm.anthropic_adapter import AnthropicAdapter

    monkeypatch.setattr(
        AnthropicAdapter,
        "_client",
        lambda self: SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="no tool use here")]
                )
            )
        ),
    )
    cfg = Config(root=tmp_path, db_path=tmp_path / "kb.sqlite", provider="anthropic",
                 model="m", api_key="sk-ant-SECRETVALUE0123456789", base_url=None)
    adapter = AnthropicAdapter(cfg)
    call = {
        "extract_facts": lambda: adapter.extract_facts(source_text="x"),
        "translate_query": lambda: adapter.translate_query(question="q", qid=1),
        "extract_query_intent": lambda: adapter.extract_query_intent(question="q"),
    }[method]

    with pytest.raises(LLMOutputError, match="no tool_use block"):
        call()


REINTERPRETED_QUESTION = "What is Sample Person's birth place?"


def _kb_with_an_empty_plan(root: Path) -> Store:
    """The state that reaches `_reinterpret_empty_plan`, built from facts.

    The deterministic parser UNDERSTANDS this question, so the flow does not ask
    the provider for an intent; the plan then comes out EMPTY because the
    relation is absent from the schema, and only THEN is the provider asked to
    re-read it. That ordering is the whole point: the row's verdict and its
    reason are decided before any request is made.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "kb.sqlite")
    store.init_schema()
    store.add_fact("Sample Person", "born_in", "Sample Place", status="confirmed")
    store.add_question(REINTERPRETED_QUESTION)
    return store


def test_an_unusable_reinterpretation_is_recorded_like_any_other_arrived_answer(tmp_path):
    """THE THIRD `except LLMError` EXIT, which shipped without the discriminator.

    `_reinterpret_empty_plan` constructed its result without `output_unusable`,
    and the field defaults to the SUPPRESSING value, so every failure at this
    exit was classified an infrastructure fault. An answer that arrived and was
    unusable therefore wrote no row here while the identical answer at the two
    exits above wrote one -- and this is the exit whose reason quotes the
    provider's own words, so the page denied the run had happened while printing
    them. The two exits above were pinned; this one was not, which is how it
    survived two revisions and three gate rounds.
    """
    root = tmp_path / "kb"
    store = _kb_with_an_empty_plan(root)

    results = translate_questions(store, _AnswersUnusably(), root=root)

    assert results[0]["infrastructure_fault"] is False
    row = store.questions()[0]
    assert str(row["status"]) == "review_required"
    # The DETERMINISTIC verdict is what the row keeps -- the engine reached it
    # without the provider -- with the failed re-reading appended to it.
    assert "is not in the schema or its aliases" in str(row["reason"])
    assert "did not match schema" in str(row["reason"])


def test_a_reinterpretation_that_never_landed_is_still_not_recorded(tmp_path):
    """The counterweight, and the reason the test above is not vacuous.

    Setting `output_unusable=True` unconditionally at that exit would satisfy
    every assertion above and re-record infrastructure faults, so the negative
    needs its own run. A provider that was never reached leaves the row
    `pending`, and the question is re-picked on the next run rather than parked:
    measured, a second run once the provider answers reaches `review_required`
    with the same deterministic reason.
    """
    root = tmp_path / "kb"
    store = _kb_with_an_empty_plan(root)

    results = translate_questions(store, _NeverReached(), root=root)

    assert results[0]["infrastructure_fault"] is True
    assert _statuses(store) == ["pending"]
    assert not str(store.questions()[0]["reason"])

    # Recovery, in the same test, because "left pending" is only defensible if
    # the question comes back: `translate_questions` re-picks `pending`.
    translate_questions(store, _AnswersUnusably(), root=root)
    assert _statuses(store) == ["review_required"]
    assert "is not in the schema or its aliases" in str(store.questions()[0]["reason"])


def test_openai_inherits_the_class_without_an_edit_to_its_adapter():
    """#592 touches `openai_adapter.py` only for the redaction line -- the three
    parser call sites there needed NO change, and this is why.

    The parsers raise the subclass and `parsed_under_redaction` preserves it, so
    every adapter that routes output through a parser inherits the distinction
    for free. `OpenRouterAdapter` subclasses `OpenAIAdapter` and overrides none
    of the three, so it inherits it too. That is the argument for putting the
    class in the shared primitive rather than in each adapter, and it is
    asserted here rather than left as an inference from the file list.
    """
    import types

    from verinote.llm.openai_adapter import OpenAIAdapter

    cfg = Config(root=Path("/tmp"), db_path=Path("/tmp/x"), provider="openai",
                 model="m", api_key="sk-SECRET", base_url=None)
    adapter = OpenAIAdapter(cfg)
    message = types.SimpleNamespace(content='{"kind": "lookup_object"}')
    response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])
    completions = types.SimpleNamespace(create=lambda **kwargs: response)
    adapter._client = lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=completions)
    )
    adapter._rendered = lambda *args, **kwargs: "prompt"

    with pytest.raises(LLMOutputError):
        adapter.extract_query_intent(question="q")
