# SPDX-License-Identifier: MPL-2.0
"""What a fact row offers a reviewer, per status.

The row template is the UI contract for review decisions. A rejected fact
(`superseded`) has left the KB by a human's deliberate act; the row that comes
back from that decision must not offer a one-click way back in. Undo, if it is
ever wanted, has to be its own explicit and audited action -- not the same
`needs_review ⇄ confirmed` button the reviewer just used.

The control cases matter as much: strip the buttons from a status the reviewer
still has to act on and review becomes impossible. So the controls run over
*every* status in the two tiers -- read from the tier accessors at call time,
not copied here -- and not just the one status a hand-written case happened to
pick. `candidate` in particular is what extraction stamps on a new fact, so it
is the bulk of the queue; a control that only looked at `needs_review` would
watch the buttons vanish from most of the review page and still pass.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.store.tiers import engine_statuses, review_statuses  # noqa: E402
from verinote.web import create_app  # noqa: E402

REVERT_ACTIONS = ("toggle", "accept")
ALL_ACTIONS = ("toggle", "accept", "reject", "edit")


def actionable_statuses() -> list[str]:
    """Every status a reviewer must still be able to act on, at call time.

    Widening a tier widens this control set with it, so a new status arrives
    with the buttons already guarded.
    """
    return sorted(review_statuses() | engine_statuses())


def _client(tmp_path) -> TestClient:
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    client.store = app.state.store
    client.fact_id = client.store.add_fact(
        "A", "is_a", "B", status="needs_review", confidence=0.9
    )
    return client


def _offered(body: str, fact_id: int) -> set[str]:
    """The fact-level actions a row body wires up, by endpoint."""
    return {
        action
        for action in ALL_ACTIONS
        if f"/facts/{fact_id}/{action}" in body
    }


def test_rejected_row_offers_no_way_back(tmp_path):
    c = _client(tmp_path)
    body = c.post(f"/facts/{c.fact_id}/reject").text

    assert "superseded" in body
    assert _offered(body, c.fact_id) & set(REVERT_ACTIONS) == set()


def test_rejected_row_offers_no_fact_actions_at_all(tmp_path):
    # reject is a no-op on an already-rejected fact, and amend leaves the status
    # alone -- neither has a meaning here, so neither is drawn.
    c = _client(tmp_path)
    body = c.post(f"/facts/{c.fact_id}/reject").text

    assert _offered(body, c.fact_id) == set()
    # ...and the row says why it is empty. This sentence is the only thing left
    # where four buttons were: drop it and the reviewer is looking at a blank
    # cell with no account of what happened to it.
    assert "rejected — no further action" in body


def test_rejected_row_stays_inspectable(tmp_path):
    # A rejected fact is still evidence: the trust dossier stays reachable.
    c = _client(tmp_path)
    body = c.post(f"/facts/{c.fact_id}/reject").text

    assert f"/facts/{c.fact_id}/provenance" in body
    assert "badge badge-superseded" in body


def test_rejected_row_stays_stripped_when_re_rendered(tmp_path):
    # Not a property of the reject response: any render of a superseded row.
    c = _client(tmp_path)
    c.post(f"/facts/{c.fact_id}/reject")
    body = c.get(f"/facts/{c.fact_id}/row").text

    assert "superseded" in body
    assert _offered(body, c.fact_id) == set()


@pytest.mark.parametrize("status", actionable_statuses())
def test_actionable_row_keeps_every_action(tmp_path, status):
    # Control, over both tiers: strip these and the queue cannot be reviewed
    # (review tier) or a promoted fact can never be demoted again (engine
    # tier). This issue is about rejection, not about freezing anything else.
    c = _client(tmp_path)
    fact_id = c.store.add_fact("C", "is_a", "D", status=status, confidence=0.9)
    body = c.get(f"/facts/{fact_id}/row").text

    assert status in body
    assert _offered(body, fact_id) == set(ALL_ACTIONS)


def test_queued_row_in_the_review_page_keeps_every_action(tmp_path):
    c = _client(tmp_path)
    body = c.get("/review").text

    assert _offered(body, c.fact_id) == set(ALL_ACTIONS)
