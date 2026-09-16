# SPDX-License-Identifier: MPL-2.0
"""#606. A run that tried the provider but never reached it leaves a durable trace.

#592 settled the half that is easy to get wrong: the fault is REPORTED, never
RECORDED on a question row (a `pending` row that claims `translation_failed`
would be a lie, because the provider produced no output to have failed). But
#592's report dies with the process -- the web banner dies with the POST
response, the exit code dies with the shell -- so after a reload there is
NOWHERE that says "tried, but never reached the provider". That is the gap this
file pins: a durable, append-only `unreached_attempts` record that

  * distinguishes the FOUR populations -- `policy`, `credentials`,
    `unknown_provider`, `unreachable` -- instead of lumping them (AC-2);
  * is readable from BOTH the web Questions page and `verinote status` (AC-3);
  * carries a redacted detail and never a `__cause__` secret (AC-4); and
  * leaves `questions.status` untouched (AC-1).

RED PRE-FIX (AC-5): every "a row exists with population X" assertion below
fails before the fix, because the table, the store methods, and the
`LLMError.population` marker do not exist yet (collection/attribute errors).
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import verinote.llm as llm_pkg
from verinote.config import Config, CredentialsCorruptError
from verinote.llm.base import (
    LLMError,
    LLMOutputError,
    UNREACHED_POPULATIONS,
    client_api_key,
    redact_secret,
    unreached_population,
)
from verinote.llm.anthropic_adapter import AnthropicAdapter
from verinote.llm.factory import get_client
from verinote.llm.openai_adapter import OpenAIAdapter
from verinote.llm.openrouter_adapter import OpenRouterAdapter
from verinote.pipeline.ask import ask_question
from verinote.pipeline.query import translate_questions
from verinote.pipeline.repair import repair_question
from verinote.store import Store
from verinote.web.app import create_app

ALIAS_HEALTHY = "- 소속 -> member_of\n"
TYPED_HEALTHY = "- 자본금: amount as capital\n"
# A typed-relations file the trust-policy guard cannot read: one alias used for
# two relations. This is the SAME broken-file fixture #592's suite uses for the
# `policy` population, so the population label here means what it means there.
TYPED_BROKEN = "- 자본금: amount as capital\n- 자산: amount as capital\n"

SECRET = "sekret-1234567890"


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


def _cfg(root: Path, **kw) -> Config:
    base = dict(
        root=root,
        db_path=root / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    base.update(kw)
    return Config(**base)


def _unreached_rows(store: Store) -> list[sqlite3.Row]:
    return store.recent_unreached_attempts(100)


def _populations(store: Store) -> list[str]:
    return [str(r["population"]) for r in _unreached_rows(store)]


class _NeverReached:
    """The provider is never reached: the adapter fails before sending.

    A plain, UNMARKED `LLMError`, so it classifies to the residual
    `unreachable` -- the population this file's first assertions pin.
    """

    name = "stub"

    def extract_query_intent(self, *, question, schema_hint=""):
        raise LLMError("stub requires an API key; set VERINOTE_STUB_API_KEY")

    def translate_query(self, *, question, qid, schema_hint=""):
        raise LLMError("stub requires an API key; set VERINOTE_STUB_API_KEY")


class _NeverReachedWithKey:
    """Like `_NeverReached`, but built with a `cfg.api_key` so the record site
    has a key to redact with, and the message carries that key. This is the
    NB5 shape: an adapter that did NOT redact at construction (claude_cli /
    ollama) -- the record site must catch it.

    A plain, UNMARKED `LLMError`, so it classifies to `unreachable`.
    """

    name = "stub"

    def __init__(self) -> None:
        self.cfg = _cfg(Path("."), api_key=SECRET)

    def extract_query_intent(self, *, question, schema_hint=""):
        raise LLMError(f"stub 401 echoed {SECRET}")

    def translate_query(self, *, question, qid, schema_hint=""):
        raise LLMError(f"stub 401 echoed {SECRET}")


# ---------------------------------------------------------------------------
# U1. The four populations are real, and the classifier names them.
# ---------------------------------------------------------------------------

def test_the_closed_set_is_the_four_populations():
    # AC-2's vocabulary, pinned so a fifth label cannot sneak in and a real one
    # cannot be renamed away.
    assert set(UNREACHED_POPULATIONS) == {
        "policy",
        "credentials",
        "unknown_provider",
        "unreachable",
    }


def test_classifier_names_credentials_and_unknown_provider_from_the_marker():
    e = LLMError("no key")
    e.population = "credentials"
    assert unreached_population(e) == "credentials"
    e2 = LLMError("bad provider")
    e2.population = "unknown_provider"
    assert unreached_population(e2) == "unknown_provider"


def test_classifier_is_unreachable_for_everything_it_cannot_name():
    # The residual is `unreachable`, not a new "unknown" label: a plain
    # unmarked `LLMError`, an `LLMOutputError`, and a non-LLM exception all
    # classify to the one true statement -- a request was tried and did not
    # arrive (or there was no provider to try).
    assert unreached_population(LLMError("boom")) == "unreachable"
    assert unreached_population(LLMOutputError("boom")) == "unreachable"
    assert unreached_population(RuntimeError("boom")) == "unreachable"


def test_classifier_never_returns_policy_from_an_exception():
    # `policy` is set at the flow's policy exit, where there is no exception to
    # classify. A marker of `policy` on an exception would be a marking bug, and
    # laundering it into the record is exactly what this forbids.
    e = LLMError("boom")
    e.population = "policy"
    assert unreached_population(e) == "unreachable"


def test_adapter_sites_mark_their_population():
    cfg = _cfg(Path("."), api_key=None)
    # credentials: no usable key to authenticate with.
    with pytest.raises(LLMError) as exc_openai:
        OpenAIAdapter(cfg)._require_key()
    assert unreached_population(exc_openai.value) == "credentials"
    with pytest.raises(LLMError) as exc_anthropic:
        AnthropicAdapter(cfg)._require_key()
    assert unreached_population(exc_anthropic.value) == "credentials"
    # unreachable: a request/client failure, redacted at construction.
    assert (
        unreached_population(OpenAIAdapter(cfg)._request_failed(RuntimeError("x")))
        == "unreachable"
    )
    assert (
        unreached_population(AnthropicAdapter(cfg)._client_failed(RuntimeError("x")))
        == "unreachable"
    )


def test_openrouter_inherits_the_openai_markers():
    cfg = _cfg(Path("."), api_key=None)
    with pytest.raises(LLMError) as exc:
        OpenRouterAdapter(cfg)._require_key()
    assert unreached_population(exc.value) == "credentials"


def test_factory_marks_unknown_provider():
    cfg = _cfg(Path("."), provider="nosuchprovider")
    with pytest.raises(LLMError) as exc:
        get_client(cfg)
    assert unreached_population(exc.value) == "unknown_provider"


def test_the_marker_survives_redaction_relabel():
    # `parsed_under_redaction` relabels `LLMError`s on the parse path. The
    # marker must survive that relabel (it is an attribute, not part of `args`),
    # because a request-failure marker that is later re-raised through a redacting
    # guard would otherwise be silently demoted to the residual.
    e = LLMError(f"provider echoed {SECRET}")
    e.population = "credentials"
    assert client_api_key(OpenAIAdapter(_cfg(Path("."), api_key=SECRET))) == SECRET
    # Directly: relabel by rewriting args the same way the guard does.
    redacted = redact_secret(str(e), SECRET)
    e.args = (redacted,)
    assert e.population == "credentials"
    assert unreached_population(e) == "credentials"
    assert SECRET not in str(e) and "***" in str(e)


# ---------------------------------------------------------------------------
# U1. The durable place: append-only, four-population, redacted.
# ---------------------------------------------------------------------------

def test_record_roundtrip_and_append_only(tmp_path):
    store = _kb(tmp_path / "kb")
    qid = int(store.questions()[0]["id"])
    row_id = store.record_unreached_attempt(qid, "unreachable", "a redacted detail")
    assert isinstance(row_id, int) and row_id >= 1
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert rows[0]["population"] == "unreachable"
    assert rows[0]["question_id"] == qid
    assert rows[0]["detail"] == "a redacted detail"
    # Append-only: UPDATE and DELETE both raise (the audit trail is not mutable).
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE unreached_attempts SET detail = 'x' WHERE id = ?", (row_id,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("DELETE FROM unreached_attempts WHERE id = ?", (row_id,))


def test_invalid_population_is_rejected(tmp_path):
    store = _kb(tmp_path / "kb")
    with pytest.raises(ValueError):
        store.record_unreached_attempt(None, "bogus", "detail")
    with pytest.raises(ValueError):
        store.record_unreached_attempt(None, None, "detail")


def test_question_id_null_and_survives_question_delete(tmp_path):
    store = _kb(tmp_path / "kb")
    qid = int(store.questions()[0]["id"])
    # NULL question_id: a run-level fault before any question could be tried.
    store.record_unreached_attempt(None, "unknown_provider", "run-level detail")
    # Per-question: a row that must outlive its question (ON DELETE SET NULL).
    store.record_unreached_attempt(qid, "unreachable", "per-question detail")
    store.delete_question(qid)
    rows = _unreached_rows(store)
    assert len(rows) == 2  # both audit rows survive the question's delete
    # The per-question row outlives its question: the table's own ON DELETE
    # SET NULL unlinks it (FK to NULL), but the row, its population and its
    # detail are untouched -- the append-only refusal did not fire for the
    # schema's own propagation, and no row was lost.
    assert all(r["question_id"] is None for r in rows)
    assert {r["population"] for r in rows} == {"unknown_provider", "unreachable"}
    assert sorted(r["detail"] for r in rows) == [
        "per-question detail",
        "run-level detail",
    ]


def test_crafted_question_id_null_update_is_refused(tmp_path):
    # #608 AC-3. The append-only carve-out is exactly the FK's own `SET NULL`:
    # it may move `question_id` NOT NULL -> NULL and nothing else. A crafted
    # statement that rides that transition to also rewrite `detail` or
    # `population` must still be refused -- the pre-#608 trigger let it through,
    # so this test is red on the parent commit.
    store = _kb(tmp_path / "kb")
    qid = int(store.questions()[0]["id"])
    row_id = store.record_unreached_attempt(qid, "unreachable", "original detail")
    for mutated in (
        "UPDATE unreached_attempts SET question_id = NULL, detail = 'attacker text' WHERE id = ?",
        "UPDATE unreached_attempts SET question_id = NULL, population = 'policy' WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(mutated, (row_id,))
    # Both statements aborted: the row, and its question link, are untouched.
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert rows[0]["question_id"] == qid
    assert rows[0]["population"] == "unreachable"
    assert rows[0]["detail"] == "original detail"


def test_init_schema_repairs_the_wider_carve_out(tmp_path):
    # #608 migration. A KB opened before the tightening carries the wider
    # trigger, under which the crafted UPDATE above is ALLOWED. `init_schema()`
    # must replace that trigger: the schema.sql trigger is created with
    # `IF NOT EXISTS` (concurrency-safe for parallel worker connections), so
    # the widening detection happens in the `db.py` migration -- it reads the
    # stored trigger SQL, and only when the wide pre-#608 form is present it
    # drops and recreates the tightened one -- so after a re-open the same
    # crafted UPDATE is refused.
    root = tmp_path / "kb"
    store = _kb(root)
    qid = int(store.questions()[0]["id"])
    row_id = store.record_unreached_attempt(qid, "unreachable", "original detail")
    store.close()

    # Rebuild the pre-#608 trigger: the wide carve-out (the question_id
    # transition alone). This is the state an existing KB was in before #608.
    legacy = sqlite3.connect(root / "kb.sqlite")
    legacy.execute("DROP TRIGGER IF EXISTS unreached_attempts_no_update")
    legacy.execute(
        "CREATE TRIGGER unreached_attempts_no_update "
        "BEFORE UPDATE ON unreached_attempts FOR EACH ROW "
        "WHEN NOT (OLD.question_id IS NOT NULL AND NEW.question_id IS NULL) "
        "BEGIN SELECT RAISE(ABORT, 'unreached attempts audit is append-only'); END"
    )
    legacy.commit()
    legacy.close()

    # Under the wide trigger the crafted UPDATE slips through (the bug #608 fixes).
    wide = sqlite3.connect(root / "kb.sqlite")
    wide.execute("PRAGMA foreign_keys = ON")
    wide.execute(
        "UPDATE unreached_attempts "
        "SET question_id = NULL, detail = 'attacker text' WHERE id = ?",
        (row_id,),
    )
    wide.commit()
    wide.close()
    check = sqlite3.connect(root / "kb.sqlite")
    mutated = check.execute(
        "SELECT detail, question_id FROM unreached_attempts WHERE id = ?", (row_id,)
    ).fetchone()
    check.close()
    assert mutated[0] == "attacker text" and mutated[1] is None  # the pre-#608 leak

    # Re-open through init_schema(), which must repair the trigger.
    store = Store(root / "kb.sqlite")
    store.init_schema()
    row_id2 = store.record_unreached_attempt(qid, "unreachable", "original detail")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE unreached_attempts "
            "SET question_id = NULL, detail = 'attacker text' WHERE id = ?",
            (row_id2,),
        )


# ---------------------------------------------------------------------------
# U2. translate_records the four request-path populations per question.
# ---------------------------------------------------------------------------

def test_translate_records_policy_population(tmp_path):
    # A trust-policy file the guard cannot read: no request is sent at all.
    store = _kb(tmp_path / "kb", typed=TYPED_BROKEN)
    translate_questions(store, _NeverReached(), root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]  # AC-1: the row is untouched
    assert _populations(store) == ["policy"]


def test_translate_records_credentials_population(tmp_path):
    store = _kb(tmp_path / "kb")
    client = _MarkedCredentials()
    translate_questions(store, client, root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]
    assert _populations(store) == ["credentials"]


def test_translate_records_unreachable_population(tmp_path):
    store = _kb(tmp_path / "kb")
    translate_questions(store, _NeverReached(), root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]
    assert _populations(store) == ["unreachable"]


def test_translate_records_distinct_populations_in_one_run(tmp_path):
    # AC-2's anti-lumping pin, at the RECORD level: one run, two questions,
    # two DIFFERENT request-path populations, and the record holds both as
    # separate labels. A record that lumped the populations would store one
    # label twice and fail this. (`policy` cannot mix into the same run -- the
    # trust-policy guard is KB-wide and fires before the provider for every
    # question -- so it is pinned in its own test above, and the read surfaces
    # below pin all four labels rendered together.)
    store = _kb(tmp_path / "kb")
    store.add_question("Which is credentials?")
    client = _TwoPopulations()
    translate_questions(store, client, root=tmp_path / "kb")
    assert sorted(_populations(store)) == ["credentials", "unreachable"]
    assert _statuses(store) == ["pending", "pending"]


def test_record_coexists_with_suppression(tmp_path):
    # AC-1 and the record together: the row is NOT written (status unchanged)
    # AND the durable row IS present. This is the single state that says
    # "#592's suppression and #606's record are both in force".
    store = _kb(tmp_path / "kb")
    translate_questions(store, _NeverReached(), root=tmp_path / "kb")
    assert _statuses(store) == ["pending"]
    assert len(_unreached_rows(store)) == 1


def test_repair_records_the_population(tmp_path):
    store = _kb(tmp_path / "kb")
    qid = int(store.questions()[0]["id"])
    store.set_question_query(qid, None, "review_required", "deterministic planner declined")
    repair_question(
        store, _NeverReached(), question_id=qid,
        question="What is the sample answer?", root=tmp_path / "kb",
    )
    assert _statuses(store) == ["review_required"]  # AC-1
    assert _populations(store) == ["unreachable"]


def test_repair_records_policy_population(tmp_path):
    store = _kb(tmp_path / "kb", typed=TYPED_BROKEN)
    qid = int(store.questions()[0]["id"])
    store.set_question_query(qid, None, "review_required", "deterministic planner declined")
    repair_question(
        store, _NeverReached(), question_id=qid,
        question="What is the sample answer?", root=tmp_path / "kb",
    )
    assert _statuses(store) == ["review_required"]
    assert _populations(store) == ["policy"]


def test_ask_provider_failure_leaves_no_row_and_raises_nothing(tmp_path):
    # B1 pin. `/ask` shares `_schema_aware_query_flow_result` but is OUT of
    # scope: its `ASK_QID=0` is not a `questions` row, so a record there would
    # either FK-raise or write a row that belongs to no question. The graceful
    # fallback must be preserved -- no row, no raise, an `AskResult` returned.
    store = _kb(tmp_path / "kb")
    client = _AskNeverReaches()
    result = ask_question(
        store, client, root=tmp_path / "kb", question="What is the sample answer?",
    )
    assert result is not None
    assert result.route == "fallback"
    assert _unreached_rows(store) == []  # the B1 invariant
    assert _statuses(store) == ["pending"]


# ---------------------------------------------------------------------------
# U3. Run-level arms (unknown_provider / credentials) and the read surfaces.
# ---------------------------------------------------------------------------

def test_web_translate_records_unknown_provider(tmp_path):
    root = tmp_path / "kb"
    _kb(root)
    cfg = _cfg(root, provider="nosuchprovider")
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/questions/translate", follow_redirects=False)
    assert response.status_code == 200
    assert "Translation could not run" in " ".join(response.text.split())
    assert _statuses(app.state.store) == ["pending"]
    rows = _unreached_rows(app.state.store)
    assert len(rows) == 1
    assert rows[0]["population"] == "unknown_provider"
    assert rows[0]["question_id"] is None


def test_web_translate_records_credentials_and_reraises(tmp_path, monkeypatch):
    # The CCE arm records the `credentials` population and then RE-RAISES so the
    # app-level 409 halt still renders -- the response is unchanged, the row is
    # the new part. A future edit that swallows the raise would change the
    # status code and fail this.
    root = tmp_path / "kb"
    _kb(root)
    cfg = _cfg(root, credentials_error="corrupt credentials file")
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/questions/translate", follow_redirects=False)
    assert response.status_code == 409  # the halt still renders
    rows = _unreached_rows(app.state.store)
    assert len(rows) == 1
    assert rows[0]["population"] == "credentials"
    assert rows[0]["question_id"] is None
    assert _statuses(app.state.store) == ["pending"]


def test_web_questions_page_shows_all_four_populations(tmp_path):
    # B3 pin (web). Seed ONE row per population and assert ALL FOUR distinct
    # labels appear in the rendered HTML. A lumped render would show one label
    # and go red here.
    root = tmp_path / "kb"
    store = _kb(root)
    for pop in ("policy", "credentials", "unknown_provider", "unreachable"):
        store.record_unreached_attempt(None, pop, f"detail-{pop}")
    cfg = _cfg(root)
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    page = client.get("/questions")
    text = " ".join(page.text.split())
    for pop in ("policy", "credentials", "unknown_provider", "unreachable"):
        assert pop in text, f"web render is missing the {pop!r} label"


def test_cli_status_prints_all_four_populations(tmp_path):
    # B3 pin (CLI). Same four-row seed, all four labels must appear on stdout.
    root = tmp_path / "kb"
    store = _kb(root)
    for pop in ("policy", "credentials", "unknown_provider", "unreachable"):
        store.record_unreached_attempt(None, pop, f"detail-{pop}")
    store.close()
    cfg = _cfg(root)
    from verinote.cli import _status

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _status(cfg)
    assert rc == 0
    for pop in ("policy", "credentials", "unknown_provider", "unreachable"):
        assert pop in buf.getvalue(), f"status output is missing the {pop!r} label"


def test_cli_status_degrades_on_a_pre_migration_kb(tmp_path):
    # B2 pin. A KB created before `unreached_attempts` existed has no such table
    # (and the read path may be `immutable=1`). `status` must not crash -- it
    # omits the section and says how to migrate.
    root = tmp_path / "kb"
    store = _kb(root)
    store._conn.execute("DROP TABLE unreached_attempts")
    store._conn.commit()
    store.close()
    cfg = _cfg(root)
    from verinote.cli import _status

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _status(cfg)  # must not raise
    assert rc == 0
    assert "not yet migrated" in buf.getvalue()


def test_cli_cmd_query_records_unknown_provider(tmp_path):
    root = tmp_path / "kb"
    _kb(root)
    cfg = _cfg(root, provider="nosuchprovider")
    from verinote.cli import cmd_query

    args = argparse.Namespace(question=None)
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        rc = cmd_query(cfg, args)
    assert rc == 1
    store = Store(root / "kb.sqlite")
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert rows[0]["population"] == "unknown_provider"
    assert rows[0]["question_id"] is None
    assert [str(q["status"]) for q in store.questions()] == ["pending"]


def test_cli_cmd_query_credentials_arm_is_intentional_and_pinned(tmp_path, monkeypatch):
    # NB4 pin. A corrupt credentials file used to escape `cmd_query` as an
    # unhandled traceback (only `LLMError` was caught). The arm is now
    # INTENTIONAL: record the `credentials` population, one line on stderr, rc 1.
    root = tmp_path / "kb"
    _kb(root)
    cfg = _cfg(root)

    def _raise_cce(_cfg):
        raise CredentialsCorruptError("credentials file is corrupt")

    monkeypatch.setattr(llm_pkg, "get_client", _raise_cce)
    from verinote.cli import cmd_query

    args = argparse.Namespace(question=None)
    buf_err = io.StringIO()
    with contextlib.redirect_stderr(buf_err):
        rc = cmd_query(cfg, args)
    assert rc == 1
    stderr = buf_err.getvalue().strip().splitlines()
    assert any("credentials" in line for line in stderr)
    store = Store(root / "kb.sqlite")
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert rows[0]["population"] == "credentials"
    assert rows[0]["question_id"] is None
    assert [str(q["status"]) for q in store.questions()] == ["pending"]


# ---------------------------------------------------------------------------
# U2/U3. Redaction: no secret, no `__cause__`, and the NB5 record-site gap.
# ---------------------------------------------------------------------------

def test_record_detail_is_redacted_for_an_unredacted_adapter(tmp_path):
    # NB5 pin. A client shaped like claude_cli/ollama (which do NOT redact at
    # construction) raises an `LLMError` carrying the key. The record site must
    # redact with the client's key, so the durable row holds `***`, not the key.
    store = _kb(tmp_path / "kb")
    translate_questions(store, _NeverReachedWithKey(), root=tmp_path / "kb")
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert SECRET not in rows[0]["detail"]
    assert "***" in rows[0]["detail"]


def test_record_detail_never_carries_a_cause_secret(tmp_path):
    # AC-4. The detail is the top-level message, redacted. A secret that lives
    # only in `__cause__` must never reach the row, because the record reads
    # `str(exc)`, not the chained exception.
    store = _kb(tmp_path / "kb")
    client = _CauseCarriesSecret()
    translate_questions(store, client, root=tmp_path / "kb")
    rows = _unreached_rows(store)
    assert len(rows) == 1
    assert SECRET not in rows[0]["detail"]


# ---------------------------------------------------------------------------
# Supporting fixtures and helpers.
# ---------------------------------------------------------------------------

def _statuses(store: Store) -> list[str]:
    return [str(q["status"]) for q in store.questions()]


class _MarkedCredentials:
    """The `credentials` population on the request path: the adapter fails for
    lack of a key and marks it, exactly as `_require_key` does."""

    name = "stub"

    def _raise(self):
        e = LLMError("stub requires an API key; set VERINOTE_STUB_API_KEY")
        e.population = "credentials"
        raise e

    def extract_query_intent(self, *, question, schema_hint=""):
        self._raise()

    def translate_query(self, *, question, qid, schema_hint=""):
        self._raise()


class _TwoPopulations:
    """One client, two request-path populations, keyed on the question text, so
    a single `translate_questions` run records both -- the anti-lumping proof
    that the population is classified per failure, not per run.

    `credentials` is marked on the error exactly as `_require_key` does; the
    other question fails unmarked and takes the `unreachable` residual.
    """

    name = "stub"

    def _raise(self, question: str):
        if question == "Which is credentials?":
            e = LLMError("stub requires an API key")
            e.population = "credentials"
        else:
            e = LLMError("stub is unreachable: connection refused")
        raise e

    def extract_query_intent(self, *, question, schema_hint=""):
        self._raise(question)

    def translate_query(self, *, question, qid, schema_hint=""):
        self._raise(question)


class _AskNeverReaches:
    """The `/ask` client: the flow never reaches the provider, and the fallback
    `answer_question` is present so the graceful path can complete."""

    name = "stub"

    def extract_query_intent(self, *, question, schema_hint=""):
        raise LLMError("stub requires an API key")

    def translate_query(self, *, question, qid, schema_hint=""):
        raise LLMError("stub requires an API key")

    def answer_question(self, *, question, context):
        return "a fallback answer"


class _CauseCarriesSecret:
    """An `LLMError` whose `__cause__` (not its own message) holds the secret.
    The record reads the top-level message only, so the cause must not leak."""

    name = "stub"

    def _raise(self):
        e = LLMError("stub request failed")
        e.__cause__ = ValueError(f"underlying error carried {SECRET}")
        raise e

    def extract_query_intent(self, *, question, schema_hint=""):
        self._raise()

    def translate_query(self, *, question, qid, schema_hint=""):
        self._raise()
