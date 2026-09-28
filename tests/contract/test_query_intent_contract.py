# SPDX-License-Identifier: MPL-2.0
"""Contract guards for issue #237: a role question the deterministic engine
cannot resolve must still yield a valid query intent through the provider and
the production parse boundary.

#520 widened the deterministic parser to admit the role question (`Who is the
CEO of Acme Robotics?` is now `lookup_object` carrying `expected_type="person"`,
asserted below as a precondition), but `person` has no typed-relation
verification, so the planner declines it: zero candidates (asserted as the
companion). The only thing that can turn the question into an executable intent
is therefore still the LLM — the precondition the guard rests on moved from
"the parser rejects it" to "the parser admits it and the planner yields
nothing", and both halves are asserted in the default suite below. A guard that
pushes a raw intent through ``parse_query_intent`` goes red on any branch where
the model fills ``reason`` on a ``lookup_object`` intent, the schema the
validator rejects.

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


def test_deterministic_parser_admits_and_declines_the_role_question():
    """Precondition: the deterministic engine cannot answer this question.

    #520 admits the role question to the deterministic parser — `Who is the
    CEO of Acme Robotics?` is now `lookup_object` carrying
    `expected_type="person"` — but `person` has no typed-relation verification,
    so the planner declines it to the model: the same snapshot that holds a
    `CEO` relation for the subject yields exactly one candidate for the
    type-neutral reading and zero for the `person` reading, isolating the type
    check as the only difference. The LLM remains the only thing that can turn
    the question into an executable intent, which is what the live/replay
    assertions below exercise. If either half changes, they would stop
    exercising the provider boundary and silently go vacuous.
    """
    from verinote.pipeline.query_planner import plan_query_candidates
    from verinote.pipeline.query_schema import (
        EntityRef,
        QuerySchemaSnapshot,
        RelationSchema,
        TermRef,
    )

    intent = deterministic_query_intent("Who is the CEO of Acme Robotics?")
    assert intent.kind == QueryIntentKind.LOOKUP_OBJECT
    assert intent.expected_type == "person"

    ceo = TermRef(
        display="CEO", executable='"CEO"', kind="StringLit", key="StringLit:\"CEO\""
    )
    company = EntityRef(
        display="Acme Robotics",
        executable='"Acme Robotics"',
        kind="StringLit",
        key="StringLit:\"Acme Robotics\"",
        fact_count=1,
    )
    snapshot = QuerySchemaSnapshot(
        relations=(
            RelationSchema(
                relation=ceo,
                canonical_relation="CEO",
                aliases=(),
                typed=None,
                fact_count=1,
                distinct_subject_count=1,
                distinct_object_count=1,
                subjects=(company,),
                objects=(
                    EntityRef(
                        display="Sample Person",
                        executable='"Sample Person"',
                        kind="StringLit",
                        key="StringLit:\"Sample Person\"",
                        fact_count=1,
                    ),
                ),
                subjects_truncated=False,
                objects_truncated=False,
            ),
        ),
        relations_truncated=False,
        relation_aliases=(),
        typed_relations=(),
        exact_entity_facts=(),
        exact_entity_facts_truncated=False,
        fact_count=1,
    )

    # the type-bearing reading is declined by the planner
    assert plan_query_candidates(intent, snapshot, qid=0).candidates == ()

    # the type-neutral reading of the same question still plans: the refusal
    # above is the type check, not a missing relation or subject
    neutral = QueryIntent(
        kind=QueryIntentKind.LOOKUP_OBJECT,
        subject=intent.subject,
        relation_candidates=intent.relation_candidates,
    )
    assert len(plan_query_candidates(neutral, snapshot, qid=0).candidates) == 1


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
