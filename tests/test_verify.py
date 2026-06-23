# SPDX-License-Identifier: Apache-2.0
from verinote.pipeline.verify import policy_path, verify
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_verify_gates_on_contradiction_with_default_policy(tmp_path):
    s = _store(tmp_path)
    # two distinct established_on values for one subject = functional conflict
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "established_on", "2021", status="confirmed")
    rep = verify(s)
    assert rep.errors > 0 and rep.ok is False


def test_verify_consistent_kb_passes(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "is_a", "company", status="accepted")
    rep = verify(s)
    assert rep.errors == 0 and rep.ok is True


def test_verify_only_considers_engine_statuses(tmp_path):
    s = _store(tmp_path)
    # a contradicting pair that is NOT yet confirmed must not gate
    s.add_fact("Org", "established_on", "2020", status="confirmed")
    s.add_fact("Org", "established_on", "2021", status="needs_review")
    rep = verify(s)
    assert rep.errors == 0 and rep.ok is True


def test_verify_loads_kb_policy_file(tmp_path):
    s = _store(tmp_path)
    s.add_fact("Org", "is_a", "company", status="confirmed")
    p = policy_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_has_isa(subject: symbol, object: symbol)\n"
        'error_has_isa(S, O) :- relation(S, "is_a", O).\n',
        encoding="utf-8",
    )
    rep = verify(s)
    assert rep.errors == 1
    assert "has_isa: Org company" in "\n".join(rep.findings)
