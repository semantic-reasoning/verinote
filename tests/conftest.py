# SPDX-License-Identifier: MPL-2.0
"""Shared test fakes and the environment sandbox that keeps tests off the real home.

`verinote.config.app_config_dir()` resolves through `HOME`/`USERPROFILE`/
`XDG_CONFIG_HOME`/`APPDATA`/`LOCALAPPDATA` depending on the platform, and
`active_root()` falls back to `./data` relative to the *current working
directory*. A test that forgets to isolate either one can rewrite the
developer's real `app.json` — repointing the active KB — or open the repo's own
`data/kb.sqlite`. The autouse fixture below closes both holes structurally, so
isolation is no longer something each test author has to remember.
"""

import env_sandbox
import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.query_intent import parse_query_intent


@pytest.fixture(autouse=True)
def isolate_app_environment(monkeypatch, tmp_path_factory):
    """Point every home-ish env var at a throwaway home, and CWD at a throwaway dir.

    A dedicated temp dir (not `tmp_path`) is used for the fake home: many tests
    use `tmp_path` as a KB root and walk it, so planting a home inside it would
    pollute them. `VERINOTE_ROOT` is dropped so an ambient export cannot leak in,
    and the CWD is moved off the repo so `active_root()`'s `./data` fallback
    cannot reach the repo's own KB. Tests that set these vars themselves still
    win: this runs first, their `setenv` runs after.
    """
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.delenv("VERINOTE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    env_sandbox.enter(home)
    try:
        yield home
    finally:
        env_sandbox.exit()


@pytest.fixture(scope="session", autouse=True)
def real_app_config_is_untouched():
    """Fail the session if anything wrote to the developer's real app config."""
    path = env_sandbox.REAL_APP_CONFIG_PATH
    before = env_sandbox.snapshot(path)
    yield
    after = env_sandbox.snapshot(path)
    if before is None:
        assert after is None, f"the test run created the real app config at {path}"
    else:
        assert after == before, f"the test run modified the real app config at {path}"


class FakeClient:
    """An `LLMClient` that returns canned facts (or raises) — no provider needed."""

    name = "fake"

    def __init__(
        self,
        facts=(),
        *,
        error: LLMError | None = None,
        query=None,
        intent=None,
        answer: str = "Synthetic fallback answer",
    ):
        self._facts = list(facts)
        self._error = error
        # query: callable(question, qid) -> Datalog line; default answers an is_a.
        self._query = query
        self._intent = intent
        self._answer = answer
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._facts)

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._query is not None:
            return self._query(question, qid)
        return f'answer_q{qid}(O) :- relation("{question}", "is_a", O).'

    def extract_query_intent(self, *, question: str, schema_hint: str = ""):
        self.calls += 1
        if self._error is not None:
            raise self._error
        raw = self._intent(question) if callable(self._intent) else self._intent
        if raw is None:
            raw = {
                "kind": "unknown_or_unsupported",
                "subject": None,
                "relation": None,
                "object": None,
                "relation_candidates": None,
                "operator": None,
                "value_type": None,
                "value": None,
                "reason": "not configured",
            }
        return parse_query_intent(raw)

    def answer_question(self, *, question: str, context: str) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._answer


@pytest.fixture
def fake_client():
    return FakeClient


def query_intent_payload(
    kind,
    *,
    subject=None,
    relation=None,
    object=None,
    relation_candidates=(),
    operator=None,
    value_type=None,
    value=None,
    reason=None,
):
    def target(kind, value):
        return None if value is None else {"kind": kind, "value": value}

    return {
        "kind": kind,
        "subject": target("entity", subject),
        "relation": target("relation", relation),
        "object": target("entity", object),
        "relation_candidates": list(relation_candidates),
        "operator": operator,
        "value_type": value_type,
        "value": value,
        "reason": reason,
    }


@pytest.fixture
def intent_payload():
    return query_intent_payload
