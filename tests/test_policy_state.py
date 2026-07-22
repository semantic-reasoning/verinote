# SPDX-License-Identifier: MPL-2.0
"""A lost logic policy must never read as a consistent KB (#155)."""

import pytest

import verinote.cli as cli
import verinote.pipeline.acceptance as acceptance
from verinote.engine import DEFAULT_POLICY
from verinote.pipeline.acceptance import AcceptRecommendation
from verinote.pipeline.corroboration import store_functional_relations
from verinote.pipeline.policy_state import (
    POLICY_CLI_LINE_MISSING_RECORDED,
    POLICY_CLI_LINE_PRESENT,
    POLICY_CLI_LINE_UNRECORDED_DEFAULT,
    POLICY_RELPATH,
    PolicyMissingError,
    PolicyState,
    PolicyStatus,
    policy_cli_line,
    policy_sha256,
    resolve_policy,
)
from verinote.pipeline.query import query_path
from verinote.pipeline.verify import load_policy, verify
from verinote.store import Store

# A human-written policy: `employed_by` is single-valued for a subject. This is
# exactly the rule that evaporates in #155 when the policy file is deleted.
FUNCTIONAL_POLICY = (
    ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
    ".decl functional(rel: symbol)\n"
    ".decl error_functional_conflict(subject: symbol, rel: symbol)\n"
    'functional("employed_by").\n'
    "error_functional_conflict(S, R) :-\n"
    "    relation(S, R, A), relation(S, R, B), functional(R), A != B.\n"
)


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    return store


def _write_policy(tmp_path, text: str = FUNCTIONAL_POLICY):
    path = tmp_path / POLICY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _conflicting_facts(store: Store) -> None:
    source = store.add_source("sources/a.txt")
    store.add_fact("Ada", "employed_by", "AcmeCorp", status="confirmed", source_id=source)
    store.add_fact("Ada", "employed_by", "BetaCorp", status="confirmed", source_id=source)


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")


# --- 1. the reported bug: deleting a recorded policy used to turn the KB green ---


def test_recorded_policy_catches_conflict(tmp_path):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    _conflicting_facts(store)

    rep = verify(store)
    assert rep.ok is False
    assert rep.errors == 1
    assert "functional_conflict" in "\n".join(rep.findings)


def test_deleting_a_recorded_policy_is_an_error_not_a_clean_report(tmp_path):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    _conflicting_facts(store)
    path.unlink()

    rep = verify(store)
    assert rep.ok is False
    assert rep.errors == 1
    assert any("policy_missing" in finding for finding in rep.findings)
    assert "consistent" not in rep.text
    # both recovery routes are named, and neither of them is automatic
    assert "policy reset --force" in rep.text
    assert "version control" in rep.text


def test_resolve_policy_missing_recorded(tmp_path):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    path.unlink()

    state = resolve_policy(store)
    assert state.status is PolicyStatus.MISSING_RECORDED
    assert state.marker is not None
    with pytest.raises(PolicyMissingError):
        load_policy(store)


def test_hash_mismatch_is_not_an_error(tmp_path):
    """Editing a policy is normal: the sha256 is evidence, never a verdict."""
    store = _store(tmp_path)
    _write_policy(tmp_path)
    store.record_policy_marker("0" * 64, origin="scaffold")

    state = resolve_policy(store)
    assert state.status is PolicyStatus.PRESENT
    assert verify(store).ok is True


# --- 2. fresh KB: no file, no marker -> default policy, but loudly ---


def test_unrecorded_policy_runs_default_with_a_warning(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Ada", "is_a", "engineer", status="confirmed")

    rep = verify(store)
    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings >= 1
    assert any("policy_unrecorded" in finding for finding in rep.findings)
    assert "shipped default" in rep.text
    # a run of the default policy is not a statement about this KB's rules, so
    # the engine's clean-bill sentence must not survive into the report
    assert "consistent" not in rep.text
    assert resolve_policy(store).status is PolicyStatus.UNRECORDED_DEFAULT
    assert load_policy(store) is None


def test_unrecorded_policy_still_reports_default_policy_errors(tmp_path):
    store = _store(tmp_path)
    store.add_fact("Org", "established_on", "2020", status="confirmed")
    store.add_fact("Org", "established_on", "2021", status="confirmed")

    rep = verify(store)
    assert rep.ok is False
    assert rep.errors > 0
    assert any("policy_unrecorded" in finding for finding in rep.findings)


# --- 3. init scaffolds a policy file and records the marker ---


def test_init_records_a_scaffold_marker(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0

    path = tmp_path / POLICY_RELPATH
    assert path.is_file()
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    marker = store.policy_marker()
    assert marker is not None
    assert marker["origin"] == "scaffold"
    assert marker["sha256"] == policy_sha256(DEFAULT_POLICY)
    store.close()


# --- 4. backwards compatibility: a pre-marker KB adopts its policy on write open ---


def test_existing_policy_file_is_adopted_on_write_open(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    path = _write_policy(tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    assert store.policy_marker() is None  # pre-#155 KB
    store.close()

    assert cli.main(["seed"]) == 0
    capsys.readouterr()

    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    marker = store.policy_marker()
    assert marker is not None
    assert marker["origin"] == "adopted"
    assert marker["sha256"] == policy_sha256(path.read_text(encoding="utf-8"))
    store.close()


def test_kb_with_no_policy_file_is_not_adopted(tmp_path, monkeypatch):
    """An upgraded KB that already had no policy has no evidence either way."""
    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.close()

    assert cli.main(["status"]) == 0

    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    assert store.policy_marker() is None
    assert resolve_policy(store).status is PolicyStatus.UNRECORDED_DEFAULT
    store.close()


# --- 5. a recorded-but-missing policy is never silently re-created ---


def test_init_refuses_to_recreate_a_recorded_policy(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    path = tmp_path / POLICY_RELPATH
    path.unlink()

    rc = cli.main(["init"])
    assert rc != 0
    assert not path.exists()  # re-creating it would hide the loss
    err = capsys.readouterr().err
    assert "policy reset --force" in err


def test_web_open_root_does_not_recreate_a_recorded_policy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web.app import create_app

    # keep the app-level config (active KB root) inside tmp_path
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "kb"
    root.mkdir()
    store = Store(Config.for_root(root).db_path)
    store.init_schema()
    path = _write_policy(root)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    _conflicting_facts(store)
    store.close()
    path.unlink()

    app = create_app(Config.for_root(root))
    with TestClient(app) as client:
        resp = client.post("/kb/select", data={"root": str(root)}, follow_redirects=False)
        assert resp.status_code in (200, 303)
        assert not path.exists()
        report = client.get("/report")
        assert report.status_code == 200
        assert "policy_missing" in report.text
        assert "no findings" not in report.text


# --- 6. the escape hatch: an explicit human reset ---


def test_policy_reset_requires_force(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    path = tmp_path / POLICY_RELPATH
    path.unlink()

    assert cli.main(["policy", "reset"]) == 2
    assert not path.exists()


def test_policy_reset_force_restores_the_policy(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    path = tmp_path / POLICY_RELPATH
    path.unlink()

    assert cli.main(["policy", "reset", "--force"]) == 0
    assert path.read_text(encoding="utf-8") == DEFAULT_POLICY

    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    marker = store.policy_marker()
    assert marker is not None
    assert marker["origin"] == "reset"
    rep = verify(store)
    assert rep.ok is True
    assert not any("policy_missing" in finding for finding in rep.findings)
    assert not any("policy_unrecorded" in finding for finding in rep.findings)
    store.close()


# --- 7. the second consumer: acceptance gating must not silently change ---


def test_store_functional_relations_raises_when_policy_is_lost(tmp_path):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    assert store_functional_relations(store) == {"employed_by"}

    path.unlink()
    with pytest.raises(PolicyMissingError):
        store_functional_relations(store)


def test_store_functional_relations_uses_default_for_unrecorded_kb(tmp_path):
    store = _store(tmp_path)
    assert "established_on" in store_functional_relations(store)


def test_auto_accept_is_fail_closed_when_the_policy_is_lost(tmp_path):
    """A rule-driven promotion must not run while the rules are missing."""
    from verinote.pipeline.acceptance import apply_auto_accept_recommendations

    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    source = store.add_source("sources/a.txt")
    fact_id = store.add_fact(
        "Ada", "is_a", "engineer", status="confirmed", confidence=0.99, source_id=source
    )
    path.unlink()

    with pytest.raises(PolicyMissingError):
        apply_auto_accept_recommendations(store)
    assert store.get_fact(fact_id)["status"] == "confirmed"


# --- web: a halted KB reports, it does not crash, and it does not accept writes ---


def _halted_client(tmp_path, monkeypatch):
    """A web client on a KB whose recorded policy file has been deleted."""
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web.app import create_app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    store = Store(cfg.db_path)
    store.init_schema()
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    source_id = store.add_source("sources/a.txt")
    fact_id = store.add_fact(
        "Ada", "is_a", "engineer", status="candidate", confidence=0.9, source_id=source_id
    )
    question_id = store.add_question("who is Ada?")
    store.close()
    path.unlink()

    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    client.fact_id = fact_id
    client.source_id = source_id
    client.question_id = question_id
    client.job_id = 1
    client.policy_path = path
    return client


def _get_paths(app, client) -> list[str]:
    """Every GET route the app declares, with path params filled in."""
    values = {
        "fact_id": client.fact_id,
        "source_id": client.source_id,
        "question_id": client.question_id,
        "job_id": client.job_id,
    }
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "GET" not in methods or not path or path.startswith("/static"):
            continue
        for name, value in values.items():
            path = path.replace("{" + name + "}", str(value))
        if "{" in path:  # an unknown param: fail loudly rather than skip silently
            raise AssertionError(f"route param not covered by this test: {path}")
        paths.append(path)
    return paths


def _mutating_paths(app, client) -> list[str]:
    """Every POST/PUT/PATCH/DELETE route the app declares, with params filled in."""
    values = {
        "fact_id": client.fact_id,
        "source_id": client.source_id,
        "question_id": client.question_id,
        "job_id": client.job_id,
    }
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if not path or not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        for name, value in values.items():
            path = path.replace("{" + name + "}", str(value))
        if "{" in path:
            raise AssertionError(f"route param not covered by this test: {path}")
        paths.append(path)
    return paths


def test_no_get_route_crashes_when_the_policy_is_lost(tmp_path, monkeypatch):
    """Enumerated so a newly added route is covered automatically."""
    client = _halted_client(tmp_path, monkeypatch)
    app = client.app
    exempt = {"/report", "/settings"}

    paths = _get_paths(app, client)
    assert "/" in paths and "/review" in paths and "/workbench" in paths

    for path in paths:
        resp = client.get(path)
        assert resp.status_code != 500, f"{path} crashed instead of reporting"
        if path in exempt:
            continue
        assert resp.status_code == 409, f"{path} did not halt"
        assert "policy reset --force" in resp.text, f"{path} hid the recovery route"

    report = client.get("/report")
    assert report.status_code == 200
    assert "policy_missing" in report.text
    # (the "has unresolved errors under its policy" errors banner is not a claim
    # that the KB checked out clean)
    assert "knowledge base is consistent" not in report.text


def test_report_banner_makes_no_fact_causal_claim_on_a_lost_policy(tmp_path, monkeypatch):
    """The errors banner must be accurate for every `errors > 0` cause (#164).

    A policy_missing KB short-circuits in `verify()` before the engine reads any
    fact (errors=1), so a banner that said the errors were "derived from the
    facts currently promoted" or advised "reject, correct, or demote the facts
    involved" would assert a cause the check never produced — the same false-claim
    class #164 exists to close, one layer down. Recovery here is restoring the
    policy file, not touching facts.
    """
    client = _halted_client(tmp_path, monkeypatch)

    report = client.get("/report")

    assert report.status_code == 200
    assert "policy_missing" in report.text
    # The banner renders (errors > 0) and states the two load-bearing true things,
    assert "Promotion is not blocked" in report.text
    assert "has unresolved errors under its policy" in report.text
    # but makes no fact-specific causal claim or fact-only recovery advice.
    assert "from the facts currently promoted to it" not in report.text
    assert "reject, correct, or demote the facts involved" not in report.text


def test_report_survives_a_lost_policy_when_the_kb_has_a_query(tmp_path, monkeypatch):
    """The /report policy-missing fallback, on the KB shape that actually reaches it.

    `_halted_client` has no query file, so `report_trace` returns before it ever
    consults the policy — which left the fallback below unexecuted by any test.
    It takes a halted KB that *has* a query (and engine facts to trace) to drive
    the trust lookup that raises `PolicyMissingError` inside `report_trace`. That
    hole is what let the fallback keep constructing `ReportTrace` with a field
    that no longer exists.
    """
    client = _halted_client(tmp_path, monkeypatch)
    store = client.app.state.store
    source_id = store.add_source("sources/b.txt")
    store.add_fact(
        "Ada", "born_in", "London", status="confirmed", source_id=source_id
    )
    path = query_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O).\n',
        encoding="utf-8",
    )

    report = client.get("/report")

    assert report.status_code == 200, report.text
    assert "policy_missing" in report.text
    assert "knowledge base is consistent" not in report.text


def test_halted_kb_refuses_writes_and_leaves_facts_untouched(tmp_path, monkeypatch):
    client = _halted_client(tmp_path, monkeypatch)
    store = client.app.state.store

    resp = client.post(f"/facts/{client.fact_id}/accept")

    assert resp.status_code == 409
    assert "policy reset --force" in resp.text
    # the guard runs before the route, so the autocommitted status change that
    # used to happen before the render blew up cannot happen at all
    assert store.get_fact(client.fact_id)["status"] == "candidate"


def test_no_mutating_route_writes_when_the_policy_is_lost(tmp_path, monkeypatch):
    """Enumerated like the GET test: default-deny stays locked as routes are added.

    The only writes a halted KB accepts are the ones that leave it (switching the
    active KB root). Everything else — facts *and* human-written policy files such
    as relation-aliases.md — would be a change made while this KB's rules are not
    being applied.
    """
    client = _halted_client(tmp_path, monkeypatch)
    app = client.app
    store = app.state.store
    # writes that are allowed because they are how a human gets *out* of the halt
    exempt = {"/kb/select", "/settings/root"}

    paths = _mutating_paths(app, client)
    assert f"/facts/{client.fact_id}/accept" in paths
    assert "/settings/relation-aliases" in paths and "/settings" in paths

    before = {int(f["id"]): f["status"] for f in store.facts()}
    for path in paths:
        if path in exempt:
            continue
        resp = client.post(path, data={"relation_aliases_text": "role -> ROLE"})
        assert resp.status_code == 409, f"{path} accepted a write on a halted KB"
        assert "policy reset --force" in resp.text, f"{path} hid the recovery route"

    assert {int(f["id"]): f["status"] for f in store.facts()} == before
    # no policy or settings file was written behind the halt
    assert not (tmp_path / "policy" / "relation-aliases.md").exists()
    assert not (tmp_path / "config.json").exists()
    assert not client.policy_path.exists()


def test_questions_page_exposes_a_lost_policy(tmp_path, monkeypatch):
    client = _halted_client(tmp_path, monkeypatch)

    resp = client.get("/questions")

    assert resp.status_code == 409
    assert "policy_missing" in resp.text or "is missing" in resp.text
    assert "policy reset --force" in resp.text


def test_restoring_the_policy_file_unblocks_the_kb(tmp_path, monkeypatch):
    client = _halted_client(tmp_path, monkeypatch)
    assert client.get("/review").status_code == 409

    client.policy_path.parent.mkdir(parents=True, exist_ok=True)
    client.policy_path.write_text(FUNCTIONAL_POLICY, encoding="utf-8")

    assert client.get("/review").status_code == 200


# --- the report of an unrecorded-policy KB never renders as OK ---


def test_report_of_unrecorded_policy_kb_is_not_ok(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from verinote.config import Config
    from verinote.web.app import create_app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))
    client.app.state.store.add_fact("Ada", "is_a", "engineer", status="confirmed")

    resp = client.get("/report")

    assert resp.status_code == 200
    assert ">\n    OK\n  <" not in resp.text
    assert "WARNINGS" in resp.text
    assert "policy_unrecorded" in resp.text
    assert "knowledge base is consistent" not in resp.text


# --- a halted KB takes no writes: CLI, worker, and the recovery paths that must
# --- keep working (the contract this PR claimed but did not enforce)


def _halted_cli_kb(tmp_path, monkeypatch):
    """A CLI-visible KB whose scaffolded policy file has been deleted."""
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    policy = tmp_path / POLICY_RELPATH
    policy.unlink()
    return policy


def _kb_rows(tmp_path):
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    try:
        return (len(store.sources()), len(store.facts()), len(store.questions()))
    finally:
        store.close()


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["ingest", "<src>"], id="ingest"),
        pytest.param(["seed"], id="seed"),
        pytest.param(["query", "who is Ada?"], id="query"),
        pytest.param(["sync", "<src>"], id="sync"),
        pytest.param(["repair"], id="repair"),
    ],
)
def test_cli_write_commands_refuse_on_halted_kb(tmp_path, monkeypatch, capsys, argv):
    """Every write command halts — and writes *nothing* — while the policy is lost."""
    policy = _halted_cli_kb(tmp_path, monkeypatch)
    src = tmp_path / "input.txt"
    src.write_text("Ada is_a engineer\n", encoding="utf-8")

    rc = cli.main([a.replace("<src>", str(src)) for a in argv])

    assert rc != 0
    err = capsys.readouterr().err
    assert "policy file" in err and "missing" in err
    # nothing was written: no registered source file, no rows, no query file
    assert not (tmp_path / "sources" / "input.txt").exists()
    assert not (tmp_path / "facts" / "query.dl").exists()
    assert _kb_rows(tmp_path) == (0, 0, 0)
    assert not policy.exists()  # the evidence of the loss is intact


def test_init_refuses_on_halted_kb(tmp_path, monkeypatch, capsys):
    """`init` is not exempt: re-scaffolding would paper over the lost policy.

    Rewriting the default policy here would turn a KB whose human-written rules
    are gone into a plausible-looking green KB. Recovery is an explicit human act
    (`policy reset --force`), so `init` stays a refused write — and `--seed` must
    not slip demo rows into the halted KB on its way to the refusal either.
    """
    policy = _halted_cli_kb(tmp_path, monkeypatch)

    assert cli.main(["init", "--seed"]) == 2

    assert "policy file" in capsys.readouterr().err
    assert not policy.exists()
    assert _kb_rows(tmp_path) == (0, 0, 0)


def test_status_and_coverage_survive_halt(tmp_path, monkeypatch, capsys):
    """Read-only diagnosis keeps working — a halt you cannot inspect is a brick."""
    _halted_cli_kb(tmp_path, monkeypatch)

    assert cli.main(["status"]) == 0
    assert cli.main(["coverage"]) == 0

    out = capsys.readouterr().out
    assert "KB:" in out
    assert "coverage:" in out


def test_policy_reset_force_recovers_halted_kb(tmp_path, monkeypatch, capsys):
    """The recovery path runs *on* a halted KB, and really un-halts it."""
    policy = _halted_cli_kb(tmp_path, monkeypatch)
    src = tmp_path / "input.txt"
    src.write_text("Ada is_a engineer\n", encoding="utf-8")
    assert cli.main(["ingest", str(src)]) != 0  # halted before recovery

    assert cli.main(["policy", "reset", "--force"]) == 0

    assert policy.read_text(encoding="utf-8") == DEFAULT_POLICY
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    marker = store.policy_marker()
    store.close()
    assert marker is not None and marker["origin"] == "reset"
    # and the KB accepts writes again — recovery that leaves it halted is no recovery
    capsys.readouterr()
    assert cli.main(["ingest", str(src)]) == 0
    assert (tmp_path / "sources" / "input.txt").is_file()


class _ChunkClient:
    """One fact per chunk, subject = the chunk text. Never touches the policy."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        from verinote.llm.base import ExtractedFact

        self.calls += 1
        return [ExtractedFact(source_text, "is_a", "chunk", 0.9)]


class _PolicyDeletingClient:
    """Deletes the KB's policy file just as the *second* chunk is extracted."""

    name = "fake"

    def __init__(self, policy_path):
        self.policy_path = policy_path
        self.calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        from verinote.llm.base import ExtractedFact

        self.calls += 1
        if self.calls == 2:
            self.policy_path.unlink()
        return [ExtractedFact(source_text, "is_a", "chunk", 0.9)]


def test_extraction_worker_halts_when_policy_disappears_mid_job(tmp_path):
    """A job started on a healthy KB must stop the moment the policy vanishes.

    The CLI's start-of-command check cannot see this: the job was already running
    (and the Store already open) when the file was deleted. Only a re-check at the
    per-chunk write boundary keeps chunk 2 out of a KB with no rules.
    """
    from verinote.pipeline.extract import process_extraction_job

    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    source_id = store.add_source("sources/a.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=2
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=["alpha", "beta"])

    client = _PolicyDeletingClient(path)
    with pytest.raises(PolicyMissingError):
        process_extraction_job(store, client, job_id=job_id)

    assert client.calls == 2  # the second chunk was extracted but never persisted
    facts = store.facts()
    assert [f["subject"] for f in facts] == ["alpha"]
    snippets = [e["snippet"] for f in facts for e in store.fact_evidence(f["id"])]
    assert "beta" not in snippets
    store.close()


def test_legacy_sync_halts_mid_batch_when_policy_disappears(tmp_path):
    """The unregistered-source `sync_sources` path has the same write boundary.

    `sync_sources` loops one LLM call per source under one run row; the CLI's
    start-of-command check cannot see a policy deleted *after* the batch began. A
    source reached *after* the loss must be refused at the write boundary in
    `extract_source`, while sources written *before* it stay — the partial run row
    is left for inspection, exactly as an `LLMError` mid-batch would leave it.
    """
    from verinote.pipeline.extract import sync_sources

    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")

    client = _PolicyDeletingClient(path)  # deletes the policy as source #2 is extracted
    sources = [
        ("sources/a.txt", "alpha"),
        ("sources/b.txt", "beta"),
        ("sources/c.txt", "gamma"),
    ]
    with pytest.raises(PolicyMissingError):
        sync_sources(store, client, sources, provider="fake", model="m")

    assert client.calls == 2  # source #2 was extracted but never persisted; #3 unreached
    # only the source written before the halt survives
    assert [s["path"] for s in store.sources()] == ["sources/a.txt"]
    assert [f["subject"] for f in store.facts()] == ["alpha"]
    store.close()


def test_sync_gives_a_clean_halt_diagnosis_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
    """A policy lost mid-`sync` exits with the halt message, not a raw traceback.

    `sync` clears its start-of-command halt check, then the policy vanishes while
    the batch runs (here, as the second source is extracted). The legacy
    `sync_sources` gate raises `PolicyMissingError`; `cmd_sync` must catch it, print
    the actionable recovery text, and exit rc=2 like every other halted-write
    refusal — not leak a `RuntimeError` traceback the way it used to (#246).
    """
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0
    policy = tmp_path / POLICY_RELPATH
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(exist_ok=True)
    (sources_dir / "a.txt").write_text("alpha", encoding="utf-8")
    (sources_dir / "b.txt").write_text("beta", encoding="utf-8")
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _PolicyDeletingClient(policy))

    rc = cli.main(["sync"])

    assert rc == 2  # the same code the start-of-command refusal uses
    err = capsys.readouterr().err
    assert "policy file" in err and "missing" in err
    assert "policy reset --force" in err  # the recovery route is named
    assert "Traceback" not in err  # a clean diagnosis, not a leaked stack trace
    assert not policy.exists()  # the evidence of the loss is intact


# --- #194: the three holes left in the halt --------------------------------
# --- (a) the CLI's diagnostic surface never said the KB was halted ----------


def test_policy_cli_line_covers_every_policy_status():
    """Every `PolicyStatus` has a line; a new one without a line is a KeyError.

    A blank/absent marker for an unknown state would be exactly the silent
    fallback this module exists to kill, so the lookup is deliberately total.
    """
    from pathlib import Path

    lines = {
        status: policy_cli_line(PolicyState(status=status, path=Path("policy.dl")))
        for status in PolicyStatus
    }

    assert set(lines) == set(PolicyStatus)  # no member left unlined
    assert all(line.strip() for line in lines.values())
    assert len(set(lines.values())) == len(PolicyStatus)  # states are distinguishable
    assert lines[PolicyStatus.MISSING_RECORDED] == POLICY_CLI_LINE_MISSING_RECORDED
    assert lines[PolicyStatus.PRESENT] == POLICY_CLI_LINE_PRESENT
    assert lines[PolicyStatus.UNRECORDED_DEFAULT] == POLICY_CLI_LINE_UNRECORDED_DEFAULT
    assert "HALTED" in POLICY_CLI_LINE_MISSING_RECORDED


def test_status_says_the_kb_is_halted_on_stdout(tmp_path, monkeypatch, capsys):
    """`status` on a halted KB used to read as perfectly healthy.

    The marker goes to *stdout*, with the rest of the summary: `verinote status >
    out.txt` and a CI health check read stdout, and a halt they cannot see is not
    a halt. The loud recovery text still goes to stderr.
    """
    _halted_cli_kb(tmp_path, monkeypatch)

    assert cli.main(["status"]) == 0  # diagnosis must still work *on* a halted KB

    captured = capsys.readouterr()
    assert POLICY_CLI_LINE_MISSING_RECORDED in captured.out
    assert "policy reset --force" in captured.err
    assert "version control" in captured.err


def test_coverage_says_the_kb_is_halted_on_stdout(tmp_path, monkeypatch, capsys):
    _halted_cli_kb(tmp_path, monkeypatch)

    assert cli.main(["coverage"]) == 0  # plain coverage is a recovery path: rc=0

    captured = capsys.readouterr()
    assert POLICY_CLI_LINE_MISSING_RECORDED in captured.out
    assert "coverage:" in captured.out
    assert "policy reset --force" in captured.err


def test_coverage_strict_fails_on_a_halted_kb(tmp_path, monkeypatch, capsys):
    """`--strict` is a machine gate: a KB with no rules does not pass it."""
    _halted_cli_kb(tmp_path, monkeypatch)

    assert cli.main(["coverage", "--strict"]) == 1

    captured = capsys.readouterr()
    assert POLICY_CLI_LINE_MISSING_RECORDED in captured.out
    assert "logic policy file is missing" in captured.err


def test_coverage_strict_passes_on_a_healthy_kb(tmp_path, monkeypatch, capsys):
    """The rc=1 above is the halt talking, not a coverage gap."""
    _env(monkeypatch, tmp_path)
    assert cli.main(["init"]) == 0

    assert cli.main(["coverage", "--strict"]) == 0

    assert POLICY_CLI_LINE_PRESENT in capsys.readouterr().out


def test_status_of_unrecorded_kb_prints_the_default_line(tmp_path, monkeypatch, capsys):
    """The benign state is reported as itself — not as "ok", and not as halted."""
    _env(monkeypatch, tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    store.close()

    assert cli.main(["status"]) == 0

    captured = capsys.readouterr()
    assert POLICY_CLI_LINE_UNRECORDED_DEFAULT in captured.out
    assert "HALTED" not in captured.out
    assert captured.err == ""  # the benign state is not an error


# --- (b) the worker: a mid-job halt must rewind the job, not strand it ------


class _PolicyVanishingStore(Store):
    """A Store whose policy file disappears just as the *second* chunk is dequeued.

    This is the gap the per-chunk write boundary cannot see: the policy is gone
    before chunk 2 is even claimed, and claiming a chunk (`status='running'`,
    `attempts + 1`) is itself a write to a halted KB.
    """

    def __init__(self, db_path, policy_path):
        super().__init__(db_path)
        self.policy_path = policy_path
        self.dequeues = 0
        self.claims = []

    def next_pending_chunk(self, job_id: int):
        row = super().next_pending_chunk(job_id)
        self.dequeues += 1
        if row is not None and self.dequeues == 2:
            self.policy_path.unlink()
        return row

    def mark_chunk_running(self, chunk_id: int):
        self.claims.append(chunk_id)
        return super().mark_chunk_running(chunk_id)


def _halted_mid_job_kb(tmp_path, chunks=("alpha", "beta")):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    source_id = store.add_source("sources/a.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=len(chunks)
    )
    store.add_source_chunks(job_id=job_id, source_id=source_id, chunks=list(chunks))
    store.close()
    return path, job_id


def test_chunk_is_not_claimed_on_a_kb_that_went_halted(tmp_path):
    """The gate sits *before* `mark_chunk_running` — claiming a chunk is a write."""
    from verinote.pipeline.extract import process_extraction_job

    path, job_id = _halted_mid_job_kb(tmp_path)
    store = _PolicyVanishingStore(tmp_path / "kb.sqlite", path)
    store.init_schema()
    client = _ChunkClient()

    with pytest.raises(PolicyMissingError):
        process_extraction_job(store, client, job_id=job_id)

    chunks = store.source_chunks(job_id)
    assert [c["status"] for c in chunks] == ["done", "pending"]
    # chunk 2 was never claimed: not marked running, no attempt burned, no LLM call
    assert store.claims == [int(chunks[0]["id"])]
    assert chunks[1]["attempts"] == 0
    assert client.calls == 1
    store.close()


def test_mid_job_halt_rolls_the_job_back_to_pending(tmp_path):
    """A `running` job with a `running` chunk is stranded forever — nothing resets it."""
    from verinote.pipeline.extract import process_extraction_job

    path, job_id = _halted_mid_job_kb(tmp_path)
    store = _PolicyVanishingStore(tmp_path / "kb.sqlite", path)
    store.init_schema()

    with pytest.raises(PolicyMissingError):
        process_extraction_job(store, _ChunkClient(), job_id=job_id)

    job = store.get_extraction_job(job_id)
    assert job["status"] == "pending"
    assert "policy reset --force" in job["message"]
    assert "1/2" in job["message"]
    # the work that really happened is kept; the rest is back in the queue
    assert job["completed_chunks"] == 1
    assert job["failed_chunks"] == 0
    assert job["candidate_count"] == 1
    assert store.next_pending_chunk(job_id) is not None
    store.close()


def test_mid_job_halt_run_summary_reports_the_real_counts(tmp_path):
    """A halt notice that claims "nothing was written" would be a fresh lie.

    Chunk 1's candidate facts exist and carry this run's `run_id`; a summary
    saying the run wrote nothing would make the provenance page contradict the
    facts sitting in front of the user.
    """
    from verinote.pipeline.extract import process_extraction_job

    path, job_id = _halted_mid_job_kb(tmp_path)
    store = _PolicyVanishingStore(tmp_path / "kb.sqlite", path)
    store.init_schema()

    with pytest.raises(PolicyMissingError):
        process_extraction_job(store, _ChunkClient(), job_id=job_id)

    facts = store.facts()
    assert [f["subject"] for f in facts] == ["alpha"]
    run_id = int(facts[0]["run_id"])
    summary = store.get_run(run_id)["summary"]
    assert "job progress 1/2 chunk(s)" in summary
    assert "this run wrote 1 candidate(s) from 1 chunk(s)" in summary
    assert "rolled back to pending" in summary
    assert "0 candidate" not in summary
    store.close()


def test_resumed_job_summary_counts_only_this_run(tmp_path):
    """`run_candidates`/`run_chunks` are what *this* run did — not the job's totals.

    The second run processes exactly ONE chunk and writes ONE candidate, while the
    job's progress stands at 2/3. Both numbers must appear, in separate clauses:
    "halted after 2/3 chunk(s), 1 candidate(s) written by this run" reads as "this
    run did two chunks and wrote one candidate", which is false.
    """
    from verinote.pipeline.extract import process_extraction_job

    path, job_id = _halted_mid_job_kb(tmp_path, chunks=("alpha", "beta", "gamma"))
    # first run: halts after chunk 1 (1 candidate written by that run)
    first = _PolicyVanishingStore(tmp_path / "kb.sqlite", path)
    first.init_schema()
    with pytest.raises(PolicyMissingError):
        process_extraction_job(first, _ChunkClient(), job_id=job_id)
    first.close()

    # policy restored, job resumed, and it halts again — this time after chunk 2
    _write_policy(tmp_path)
    second = _PolicyVanishingStore(tmp_path / "kb.sqlite", path)
    second.init_schema()
    with pytest.raises(PolicyMissingError):
        process_extraction_job(second, _ChunkClient(), job_id=job_id)

    job = second.get_extraction_job(job_id)
    assert job["candidate_count"] == 2  # cumulative across both runs
    summaries = [
        row["summary"] for row in second._conn.execute("SELECT summary FROM runs ORDER BY id")
    ]
    second.close()
    # the second run wrote one candidate from one chunk, not two: the summary must
    # neither inherit the job's cumulative counter nor let the job's progress be
    # read as this run's work
    assert "job progress 1/3 chunk(s)" in summaries[0]
    assert "this run wrote 1 candidate(s) from 1 chunk(s)" in summaries[0]
    assert "job progress 2/3 chunk(s)" in summaries[1]
    assert "this run wrote 1 candidate(s) from 1 chunk(s)" in summaries[1]
    # the job stands at 2/3 but this run did one chunk — the two counts must never
    # be glued into a single clause that reads as one scope
    assert "2/3 chunk(s), 1 candidate(s) written by this run" not in summaries[1]


class _AlwaysEligibleEngine:
    """A recommendation engine that reads no policy and accepts everything.

    Stands in for the engine the real one is one refactor away from being: cache
    the policy, or move functional relations into a table, and `_engine` stops
    touching `load_policy` — the accident that refuses a halted KB today.

    `facts` is the snapshot the reconciler's retraction pass walks. Empty here:
    this stub reads nothing, so it offers no accepted fact to retract, and the
    pass iterates nothing rather than needing any further stub member.
    """

    facts = ()

    def recommend(self, row):
        return AcceptRecommendation(
            fact_id=int(row["id"]),
            eligible=True,
            reasons=(),
            support_sources=("sources/a.txt", "sources/b.txt"),
            support_fact_ids=(int(row["id"]),),
            canonical_relation=str(row["relation"]),
            typed_normalization="",
        )


def _auto_accept_kb(tmp_path):
    store = _store(tmp_path)
    path = _write_policy(tmp_path)
    store.record_policy_marker(policy_sha256(path.read_text(encoding="utf-8")), origin="scaffold")
    store.add_fact("X", "is_a", "Y", status="candidate")
    return store, path


def test_auto_accept_refuses_a_halted_kb_by_its_own_guard(tmp_path, monkeypatch):
    """`apply_auto_accept_recommendations` must refuse a halted KB ITSELF (#194).

    A halted KB stops auto-accept today only by coincidence: `_engine` builds its
    single-valued set through `store_functional_relations` -> `load_policy`, which
    raises. The refusal belongs to the write entrypoint, not to whatever the
    recommendation engine happens to read on the way — so this test removes the
    coincidence (an engine that reads no policy at all) and demands the refusal
    survive. Without `assert_writable`, a KB whose rules are gone gets facts stamped
    `accepted`: a review gate that no rule was ever applied to.
    """
    store, path = _auto_accept_kb(tmp_path)
    monkeypatch.setattr(acceptance, "_engine", lambda store: _AlwaysEligibleEngine())
    path.unlink()  # the KB recorded a policy and the file is gone: halted

    with pytest.raises(PolicyMissingError):
        acceptance.apply_auto_accept_recommendations(store)

    assert [f["status"] for f in store.facts()] == ["candidate"]
    events = [e["event_type"] for e in store.fact_events(int(store.facts()[0]["id"]))]
    store.close()
    assert "auto_accept_applied" not in events


def test_auto_accept_still_accepts_on_a_healthy_kb(tmp_path, monkeypatch):
    """The control: the guard refuses a *halted* KB, it does not disable auto-accept.

    Without this, a stub engine that quietly recommended nothing would make the test
    above pass for the wrong reason.
    """
    store, _ = _auto_accept_kb(tmp_path)
    monkeypatch.setattr(acceptance, "_engine", lambda store: _AlwaysEligibleEngine())

    applied = acceptance.apply_auto_accept_recommendations(store)

    assert [r.fact_id for r in applied] == [int(store.facts()[0]["id"])]
    assert [f["status"] for f in store.facts()] == ["accepted"]
    store.close()


def test_store_has_no_public_meta_delete(tmp_path):
    """No public API may drop a policy marker — that is the silent-fallback bug."""
    store = _store(tmp_path)
    public = {name for name in dir(store) if not name.startswith("_")}
    store.close()

    assert "delete_meta" not in public
    assert "set_meta" not in public
    assert "get_meta" not in public
    assert "clear_policy_marker" not in public
