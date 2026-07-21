# SPDX-License-Identifier: MPL-2.0
"""Guards for the DuckDB inference cache's fingerprint/relation invariant.

The base `relation` table and its fingerprint must stay in sync even when a
reload fails partway. If a reload raises after the DELETE, the fingerprint must
be left invalid so that a later run with the *same* facts reloads instead of
trusting a now-empty table and reporting a false "consistent" result.
"""

import pytest

import verinote.engine.duckdb_backend as duckdb_backend
from verinote.engine.duckdb_backend import DuckDBInferenceCache

# Same subject established on two different dates: the default policy's
# functional-conflict rule flags this, so a correctly loaded relation yields
# ok is False. An empty/unloaded relation would silently report ok is True.
_CONFLICT_FACTS = [
    {"subject": "Org", "relation": "established_on", "object": "2020"},
    {"subject": "Org", "relation": "established_on", "object": "2021"},
]
_OTHER_FACTS = [{"subject": "Org", "relation": "is_a", "object": "company"}]


def _warn_dead_functional(relation: str) -> str:
    """The report line for a `functional("rel")` decl no engine fact satisfies."""
    return (
        f'WARN dead_rule: policy declares functional("{relation}") '
        "but no engine fact uses that relation"
    )


# The conflict facts use only established_on, so the default policy's other two
# functional decls (born_on/died_on) are unused dead rules that surface as
# non-blocking WARNs beside the real conflict (issue #286).
_CONFLICT_FINDINGS = [
    "ERROR functional_conflict: Org established_on",
    _warn_dead_functional("born_on"),
    _warn_dead_functional("died_on"),
]


def _duckdb():
    return pytest.importorskip("duckdb")


def test_cache_reloads_after_a_failed_reload(monkeypatch):
    _duckdb()
    cache = DuckDBInferenceCache()
    try:
        # 1) Load the conflict facts cleanly: the conflict is detected.
        first = cache.run_check(_CONFLICT_FACTS)
        assert first.ok is False
        assert first.findings == _CONFLICT_FINDINGS

        # 2) Inject a one-shot failure into the reload triggered by different
        #    facts. The DELETE clears the base relation, then the load raises,
        #    so the run returns a fail-closed engine error.
        boom = "injected reload failure"

        def raising_load(con, facts):
            raise RuntimeError(boom)

        monkeypatch.setattr(duckdb_backend, "_load_relation_facts", raising_load)
        failed = cache.run_check(_OTHER_FACTS)
        assert failed.ok is False
        assert failed.findings == [f"ERROR internal engine error: {boom}"]

        # 3) Undo the injection and re-run the SAME conflict facts on the SAME
        #    cache instance. The reload must run again (the base relation was
        #    emptied by the failed run), so the conflict is detected once more.
        monkeypatch.undo()
        again = cache.run_check(_CONFLICT_FACTS)
        assert again.ok is False
        assert again.findings == _CONFLICT_FINDINGS
    finally:
        cache.close()
