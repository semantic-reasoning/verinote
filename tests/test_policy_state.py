# SPDX-License-Identifier: MPL-2.0
"""A lost logic policy must never read as a consistent KB (#155)."""

import pytest

import verinote.cli as cli
from verinote.engine import DEFAULT_POLICY
from verinote.pipeline.corroboration import store_functional_relations
from verinote.pipeline.policy_state import (
    POLICY_RELPATH,
    PolicyMissingError,
    PolicyStatus,
    policy_sha256,
    resolve_policy,
)
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


# --- 4. backwards compatibility: a pre-marker KB adopts its policy on open ---


def test_existing_policy_file_is_adopted_on_open(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    path = _write_policy(tmp_path)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    assert store.policy_marker() is None  # pre-#155 KB
    store.close()

    assert cli.main(["status"]) == 0

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
