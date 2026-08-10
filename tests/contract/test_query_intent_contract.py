# SPDX-License-Identifier: MPL-2.0
"""Contract guards for issue #237: a role question the deterministic parser cannot
resolve must still yield a valid query intent through the provider and the
production parse boundary.

The deterministic parser deliberately returns ``unknown_or_unsupported`` for a
"who is the CEO of X" question (asserted below as a precondition), so the only
thing that can turn it into an executable intent is the LLM. A guard that pushes
a raw intent through ``parse_query_intent`` goes red on any branch where the
parser rejects it — for example when the model fills ``reason`` on a
``lookup_object`` intent, the schema the parser rejects.

The #237 fix is merged, so the replays below run in the **default** suite, and
neither of them needs a provider, credentials or the network (issue #270).
``test_replay_raw_intent_parses_through_production_boundary`` is the one that
crosses the boundary: it reads a response captured from a real provider off disk
and pushes it through ``parse_query_intent``.
``test_claudecli_replay_retains_reason_regression_shape`` never reaches the
parser. It is the non-vacuity pin: it asserts the capture still holds the
populated ``reason`` that made #237 reproduce, without which parsing that
capture would prove nothing.

``test_live_provider_yields_valid_query_intent`` calls a provider, so it keeps
``@pytest.mark.contract`` and the opt-in gate; the precondition test and both
replays carry neither.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinote.pipeline.query_intent import (
    QueryIntent,
    QueryIntentKind,
    deterministic_query_intent,
    parse_query_intent,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "contract"
LIVE_FIXTURES = tuple(sorted(FIXTURES_DIR.glob("*/query_intent_acme_ceo.json")))


def _fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_deterministic_parser_does_not_resolve_the_role_question():
    """Precondition: the deterministic parser hands this question off to the LLM.

    Locks the assumption the whole guard rests on. If the deterministic parser
    ever starts resolving this, the live/replay assertions below would stop
    exercising the provider boundary and silently go vacuous.
    """
    intent = deterministic_query_intent("Who is the CEO of Acme Robotics?")
    assert intent.kind == QueryIntentKind.UNKNOWN_OR_UNSUPPORTED


@pytest.mark.contract
def test_live_provider_yields_valid_query_intent(require_live_provider):
    client = require_live_provider
    intent = client.extract_query_intent(question="Who is the CEO of Acme Robotics?")
    assert isinstance(intent, QueryIntent)
    assert intent.kind != QueryIntentKind.UNKNOWN_OR_UNSUPPORTED


@pytest.mark.parametrize("fixture_path", LIVE_FIXTURES, ids=lambda path: path.parent.name)
def test_replay_raw_intent_parses_through_production_boundary(fixture_path):
    fixture = _fixture(fixture_path)
    raw = fixture["raw_response"]
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(decoded, dict), "query-intent raw response must be an object"
    intent = parse_query_intent(raw)
    assert isinstance(intent, QueryIntent)
    assert intent.kind != QueryIntentKind.UNKNOWN_OR_UNSUPPORTED


def test_claudecli_replay_retains_reason_regression_shape():
    """Keep the captured #237 Claude response regression-specific assertion."""
    fixture_path = FIXTURES_DIR / "claudecli" / "query_intent_acme_ceo.json"
    fixture = _fixture(fixture_path)
    raw = fixture["raw_response"]
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    # Non-vacuity: the capture must actually hold the #237 failure shape — a
    # populated `reason` on a lookup intent — or this replay proves nothing.
    assert decoded.get("reason"), (
        "fixture does not capture the #237 failure shape (reason must be set)"
    )
