# SPDX-License-Identifier: MPL-2.0
"""Shared test fakes and the environment sandbox that keeps tests off the real home.

`verinote.config.app_config_dir()` resolves through `HOME`/`USERPROFILE`/
`XDG_CONFIG_HOME`/`APPDATA`/`LOCALAPPDATA` depending on the platform, and
`active_root()` falls back to `./data` relative to the *current working
directory*. A test that forgets to isolate either one can rewrite the
developer's real `app.json` — repointing the active KB — or open the repo's own
`data/kb.sqlite`. `Config.for_root()` also reads every `VERINOTE_*` variable
env-first, so an ambient export in the developer's shell can change what a test
sees.

The sandbox is two-tier on purpose:

* `pytest_configure` seals the environment at *session start*, before any
  fixture (of any scope) or any collected module's import-time code runs.
  A function-scoped `monkeypatch` cannot do this: pytest instantiates
  higher-scoped fixtures first, so a `scope="module"`/`"session"` fixture that
  called `Config.load()` would run *before* a function-scoped sandbox and reach
  the real home.
* The autouse fixture below then lays a fresh per-test home and CWD on top, so
  tests cannot see each other's writes.

Either tier alone leaves a hole; both together mean no test — whatever its
fixture scopes — can reach the real config.
"""

import os
import shutil
import tempfile
from pathlib import Path

import env_sandbox
import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline.query_intent import parse_query_intent

HOME_VARS = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA")

_session_patch: pytest.MonkeyPatch | None = None
_session_tmp: str | None = None


def _sandbox_env(patch: pytest.MonkeyPatch, home: Path, cwd: Path) -> None:
    """Point the home-ish vars at `home`, drop every `VERINOTE_*`, chdir to `cwd`."""
    for var in HOME_VARS:
        patch.setenv(var, str(home))
    for var in [name for name in os.environ if name.startswith("VERINOTE_")]:
        patch.delenv(var, raising=False)
    patch.chdir(cwd)


def pytest_configure(config):
    """Seal the environment before anything else in the session runs.

    Runs after this conftest (and therefore `env_sandbox`) is imported, so the
    *real* paths are still captured under the ambient environment — but before
    collection, fixtures of any scope, and test-module import-time code.
    """
    global _session_patch, _session_tmp
    _session_tmp = tempfile.mkdtemp(prefix="verinote-session-")
    home = Path(_session_tmp) / "home"
    cwd = Path(_session_tmp) / "cwd"
    home.mkdir()
    cwd.mkdir()
    _session_patch = pytest.MonkeyPatch()
    _sandbox_env(_session_patch, home, cwd)
    env_sandbox.seal(home)


def pytest_unconfigure(config):
    global _session_patch, _session_tmp
    env_sandbox.unseal()
    if _session_patch is not None:
        _session_patch.undo()
        _session_patch = None
    if _session_tmp is not None:
        shutil.rmtree(_session_tmp, ignore_errors=True)
        _session_tmp = None


@pytest.fixture(autouse=True)
def isolate_app_environment(monkeypatch, tmp_path_factory):
    """Lay a throwaway per-test home and CWD over the session-wide seal.

    A dedicated temp dir (not `tmp_path`) is used for the fake home: many tests
    use `tmp_path` as a KB root and walk it, so planting a home inside it would
    pollute them. Every `VERINOTE_*` variable is dropped (`ROOT`, `PROVIDER`,
    `MODEL`, `BASE_URL`, `API_KEY`, `LLM_TIMEOUT`, the `EXTRACTION_*` knobs and
    `AUTO_ACCEPT_RECOMMENDATIONS` are all read env-first by `Config`), and the
    CWD is moved off the repo so `active_root()`'s `./data` fallback cannot
    reach the repo's own KB. Tests that set these vars themselves still win:
    this runs first, their `setenv` runs after.
    """
    home = tmp_path_factory.mktemp("home")
    _sandbox_env(monkeypatch, home, tmp_path_factory.mktemp("cwd"))
    env_sandbox.enter(home)
    try:
        yield home
    finally:
        env_sandbox.exit()


def pytest_sessionfinish(session, exitstatus):
    """Restore, then fail the session, if anything wrote the developer's real app config.

    Detecting the leak is not enough: by now the user's `app.json` already points
    at a pytest temp KB that is about to be deleted, so `verinote ui` would open a
    KB that no longer exists. Put it back first, then fail.

    A hook, not a session fixture, and it compares against a baseline taken when
    `env_sandbox` was *imported*. A fixture is too late in both directions: its
    setup runs after collection and after every test module's import-time code,
    so a leak from there would be baked into the baseline and read as "clean";
    and its teardown never runs at all when collection fails or when no test is
    selected — exactly the runs where a stray import-time write goes unnoticed.
    """
    message = env_sandbox.leak_report(
        env_sandbox.REAL_APP_CONFIG_PATH, env_sandbox.REAL_APP_CONFIG_BEFORE
    )
    if message is None:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(message)
        return
    reporter.write_sep("=", "environment sandbox leak", red=True)
    reporter.write_line(message)


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
