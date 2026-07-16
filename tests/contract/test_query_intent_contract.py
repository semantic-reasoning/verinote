# SPDX-License-Identifier: MPL-2.0
"""Contract guard for issue #237: a role question the deterministic parser cannot
resolve must still yield a valid query intent through the *live* provider and the
production parse boundary.

The deterministic parser deliberately returns ``unknown_or_unsupported`` for a
"who is the CEO of X" question (asserted below as a precondition), so the only
thing that can turn it into an executable intent is the LLM. This guard fails on
any branch where the provider's raw intent is rejected by ``parse_query_intent``
— for example when the model fills ``reason`` on a ``lookup_object`` intent, the
schema the parser rejects. On ``origin/main`` (no #237 fix) both the live and the
replay assertions are expected to fail; that red is the point.
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

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "contract" / "query_intent_acme_ceo.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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


@pytest.mark.contract
def test_replay_raw_intent_parses_through_production_boundary(require_opt_in):
    fixture = _fixture()
    raw = json.loads(fixture["raw_response"])
    # Non-vacuity: the capture must actually hold the #237 failure shape — a
    # populated `reason` on a lookup intent — or this replay proves nothing.
    assert raw.get("reason"), "fixture does not capture the #237 failure shape (reason must be set)"
    intent = parse_query_intent(fixture["raw_response"])
    assert isinstance(intent, QueryIntent)
    assert intent.kind != QueryIntentKind.UNKNOWN_OR_UNSUPPORTED
