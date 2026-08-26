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
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import verinote.web.app as webapp
from verinote.config import Config
from verinote.llm.base import LLMError, LLMOutputError, parsed_under_redaction
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
    """The third writer. `repair_question` persists `prepared.status`, which a
    policy fault reaches as `translation_failed`."""
    root = tmp_path / "kb"
    store = _kb(root, typed=TYPED_BROKEN)
    qid = int(store.questions()[0]["id"])
    repair_question(store, _NeverReached(), question_id=qid,
                    question="What is the sample answer?", root=root)
    assert _statuses(store) == ["pending"]


def test_the_web_reports_the_fault_it_no_longer_records(tmp_path):
    """AC-2's cost, paid. The row write WAS the web's only diagnosis, so
    deleting it without this surface would have made a broken provider silent."""
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


def test_every_llm_error_subclass_takes_one_message():
    """`parsed_under_redaction` re-raises with `type(exc)(...)`, so a subclass
    whose constructor took a different shape would break inside error handling.

    Derived from `__subclasses__()` rather than listed, so a subclass added later
    is covered without anyone remembering this test exists.
    """
    subclasses = LLMError.__subclasses__()
    assert subclasses, "no subclasses -- this test would pass vacuously"
    for cls in subclasses:
        assert str(cls("probe")) == "probe", cls.__name__


def test_parsed_under_redaction_preserves_the_class_and_the_redaction():
    """Both halves together: flattening the class silently disabled the rule,
    and losing the redaction would leak a key into a question row."""
    secret = "sk-ant-SECRETVALUE0123456789"
    with pytest.raises(LLMOutputError) as excinfo:
        parsed_under_redaction(parse_query_intent, f"not json {secret}", secret)
    assert secret not in str(excinfo.value)
    assert "query intent output was not JSON" in str(excinfo.value)


def test_the_async_repair_worker_does_not_record_it_either(tmp_path):
    """THE FOURTH WRITER, EXERCISED rather than inspected.

    `persist_repair_question` is a SEPARATE write from `repair_question`'s, on
    the job worker's path, and a guard added to one does not cover the other. It
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
