# SPDX-License-Identifier: MPL-2.0
"""`superseded` is terminal, enforced by the schema (#310, #311).

Every guard before these lived in Python and read a *syntactic* property of the
source — an allowlist of method names (#292 first pass), then a regex over each
method body (#292 second pass). Both were walked past: the first by adding a
name, the second by reordering a SET clause. #309 records the hole neither can
close, because it is the shape of the technique rather than a bug in it: a write
factored into a private helper is invisible to a scan that reads one public
method at a time.

So these tests do not assert anything about how the source is written. They
attempt the forbidden write through channels that no Python guard sits on — raw
SQL on the store's own connection, spelled several ways — and assert the
database refuses. The claim under test is "the transition cannot happen", not
"the code does not appear to contain it".

Two axes, both terminal, enforced by two triggers:

  status  (#310) — a superseded fact cannot be moved to another status.
  content (#311) — a superseded fact's subject/relation/object/note cannot be
                   rewritten. A reject is a judgment about a *specific claim*,
                   so the claim is frozen with the judgment; otherwise the audit
                   log's "rejected" ends up naming text nobody rejected.

Deliberately NOT frozen: `stale` and `updated_at`. `add_fact_evidence()` clears
`stale=1` on every re-observation without regard to status (#329), and that path
is load-bearing. `test_re_observing_a_superseded_fact_still_clears_stale` is the
regression guard for that carve-out — freezing the whole row would break it.

Finally, terminality is a different axis from tiering, and this must stay true:
see `test_superseded_belongs_to_neither_status_tier`.
"""

import sqlite3

import pytest

from verinote.store import Store, TerminalFactError
from verinote.store.db import ENGINE_STATUSES, REVIEW_STATUSES

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.web import create_app  # noqa: E402

NON_TERMINAL = ("candidate", "needs_review", "confirmed", "accepted")


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _rejected_fact(store: Store) -> int:
    fact_id = store.add_fact("Ledger", "owner", "Park", status="needs_review")
    store.reject_fact(fact_id)
    assert store.get_fact(fact_id)["status"] == "superseded"
    return fact_id


def _row(store: Store, fact_id: int) -> dict:
    fact = store.get_fact(fact_id)
    return {k: fact[k] for k in fact.keys()}


def _actions(store: Store, fact_id: int) -> list[str]:
    return [event["action"] for event in store.fact_log(fact_id)]


# --- #310: the status axis is terminal at the write boundary ---------------


@pytest.mark.parametrize("target", NON_TERMINAL)
def test_raw_sql_cannot_move_a_superseded_fact_to_any_other_status(tmp_path, target):
    # The headline. This bypasses every Python guard there is — no method name to
    # add to an allowlist, no method body for a scan to read — and goes straight
    # at the connection. Parameterised across the whole non-terminal vocabulary
    # rather than one representative, so a trigger narrowed to a single status
    # (`NEW.status = 'confirmed'`, say) is red instead of green-on-the-one-case.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE facts SET status = ? WHERE id = ?", (target, fact_id)
        )

    assert store.get_fact(fact_id)["status"] == "superseded"


def test_the_refusal_does_not_depend_on_how_the_update_is_spelled(tmp_path):
    # Column order is exactly what walked past the #292 regex, so the guard that
    # replaced it must be indifferent to it. Each spelling below defeated some
    # earlier version of a source-reading guard; none of them defeat a trigger,
    # because the trigger reads the row, not the statement.
    store = _store(tmp_path)
    spellings = (
        "UPDATE facts SET status = 'confirmed' WHERE id = ?",
        # status last, with a function call and its apostrophes in front of it
        "UPDATE facts SET updated_at = datetime('now'), status = 'confirmed' "
        "WHERE id = ?",
        # lowercase keywords
        "update facts set status = 'confirmed' where id = ?",
        # no WHERE clause at all: a scan keyed on `SET ... WHERE` sees nothing
        "UPDATE facts SET status = 'confirmed'",
        # reached via a subquery rather than a literal id
        "UPDATE facts SET status = 'confirmed' "
        "WHERE id IN (SELECT id FROM facts WHERE status = 'superseded') AND id = ?",
    )

    for sql in spellings:
        fact_id = _rejected_fact(store)
        params = () if "?" not in sql else (fact_id,)
        with pytest.raises(sqlite3.IntegrityError), store._conn:
            store._conn.execute(sql, params)
        assert store.get_fact(fact_id)["status"] == "superseded", sql


def test_a_write_from_a_private_helper_is_refused_too(tmp_path):
    # #309: a status write factored out of a public method into a helper is the
    # one hole the source scan concedes it cannot see, because it reads a single
    # public method's body at a time. The trigger has no notion of where a
    # statement was called from, so the hole simply is not there. This is the
    # test that says #310 subsumes #309 rather than sitting beside it.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    def _helper_the_scan_never_reads(conn, target_id):
        conn.execute(
            "UPDATE facts SET status = 'accepted' WHERE id = ?", (target_id,)
        )

    with pytest.raises(sqlite3.IntegrityError):
        _helper_the_scan_never_reads(store._conn, fact_id)

    assert store.get_fact(fact_id)["status"] == "superseded"


def test_rejecting_a_fact_still_works(tmp_path):
    # Over-fix guard: only the way *out* is closed. reject_fact writes
    # candidate -> superseded and must stay unobstructed, or the trigger would
    # have made rejection itself impossible.
    store = _store(tmp_path)
    fact_id = store.add_fact("Ledger", "owner", "Park", status="needs_review")

    decision = store.reject_fact(fact_id)

    assert decision.changed is True
    assert store.get_fact(fact_id)["status"] == "superseded"


def test_a_same_status_rewrite_of_a_superseded_fact_is_allowed(tmp_path):
    # Over-fix guard on the WHEN clause: `OLD.status = 'superseded'` alone would
    # abort a write that sets status to the value it already holds. That is not a
    # transition and must not raise — reject_fact's own early-return relies on
    # nothing pathological happening on the replay path.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    store._conn.execute(
        "UPDATE facts SET status = 'superseded' WHERE id = ?", (fact_id,)
    )

    assert store.get_fact(fact_id)["status"] == "superseded"


@pytest.mark.parametrize("start", NON_TERMINAL)
def test_transitions_between_non_terminal_statuses_are_untouched(tmp_path, start):
    # The trigger keys on OLD.status, so every transition that does not *leave*
    # superseded must be unaffected. Without this, a trigger written as
    # `NEW.status <> 'superseded'` (dropping the OLD test) would freeze the whole
    # lifecycle and this file would still be green on everything above.
    store = _store(tmp_path)
    fact_id = store.add_fact("Ledger", "owner", "Park", status=start)

    store._conn.execute(
        "UPDATE facts SET status = 'confirmed' WHERE id = ?", (fact_id,)
    )

    assert store.get_fact(fact_id)["status"] == "confirmed"


# --- #311: the content axis is terminal too --------------------------------


def test_amend_fact_refuses_a_superseded_fact(tmp_path):
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)
    before = _row(store, fact_id)
    log_before = _actions(store, fact_id)

    with pytest.raises(TerminalFactError):
        store.amend_fact(
            fact_id, subject="Ledger", relation="owner", obj="Choi", note="typo"
        )

    # Result, not just the exception: the row is untouched and the audit log did
    # not gain an `amended` event next to the `rejected` one.
    assert _row(store, fact_id) == before
    assert _actions(store, fact_id) == log_before


def test_raw_sql_cannot_rewrite_a_superseded_fact_s_content(tmp_path):
    # The Python guard in amend_fact is diagnosis; this is the invariant. Same
    # reasoning as the status axis — a caller that reaches the connection
    # directly must still be refused.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE facts SET subject = 'Rewritten' WHERE id = ?", (fact_id,)
        )

    assert store.get_fact(fact_id)["subject"] == "Ledger"


@pytest.mark.parametrize("column", ("subject", "relation", "object", "note"))
def test_every_content_column_is_frozen(tmp_path, column):
    # Parameterised so a trigger that lists only some of the amend columns in its
    # UPDATE OF clause is red. `note` matters as much as the triple: a rejected
    # claim annotated after the fact reads as though the annotation was there
    # when the judgment was made.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            f"UPDATE facts SET {column} = 'rewritten' WHERE id = ?", (fact_id,)
        )


def test_term_token_alone_is_frozen_on_a_superseded_fact(tmp_path):
    # term_token is derived from the *typed* terms, so a kind-only amend (same
    # display text, different term kind) moves the token while leaving
    # subject/relation/object byte-identical. A trigger that omitted term_token
    # from its comparison would wave exactly that amend through, and every other
    # test in this file would stay green.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE facts SET term_token = 'rewritten-token' WHERE id = ?",
            (fact_id,),
        )


def test_a_replayed_amend_on_a_superseded_fact_is_a_quiet_no_op(tmp_path):
    # The two layers must refuse the *same* calls. An amend asking for content
    # already stored writes nothing, so the trigger (which compares values) lets
    # it pass — and the Python guard, which sits after the replay check, must let
    # it pass too. If the guard moved above that check this would raise, and the
    # layers would disagree about what "amending a superseded fact" means.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)
    before = _row(store, fact_id)

    decision = store.amend_fact(
        fact_id, subject="Ledger", relation="owner", obj="Park", note=""
    )

    assert decision.changed is False
    assert _row(store, fact_id) == before


def test_amending_a_live_fact_is_untouched(tmp_path):
    # Over-fix guard: the freeze keys on OLD.status, so amending anything that is
    # not superseded must behave exactly as before.
    store = _store(tmp_path)
    fact_id = store.add_fact("A", "is_a", "B", status="needs_review", note="orig")

    decision = store.amend_fact(
        fact_id, subject="A2", relation="became", obj="C", note="fixed"
    )

    assert decision.changed is True
    assert decision.fact["subject"] == "A2"
    assert "amended" in _actions(store, fact_id)


# --- the carve-out: stale/updated_at stay writable (#329) ------------------


def test_re_observing_a_superseded_fact_still_clears_stale(tmp_path):
    # The reason the content freeze is scoped to the amend axis instead of
    # freezing the row. note_fact_reobserved() clears stale=1 on EVERY
    # re-observation without consulting status (#329, load-bearing per its
    # docstring). A row-wide freeze would turn that into an IntegrityError the
    # moment a rejected fact's text reappeared in its source.
    store = _store(tmp_path)
    source_id = store.add_source("sources/a.txt")
    artifact_id = store.add_source_artifact(
        source_id=source_id, kind="extracted_text", path="artifacts/a.txt"
    )
    fact_id = _rejected_fact(store)
    store._conn.execute("UPDATE facts SET stale = 1 WHERE id = ?", (fact_id,))

    store.note_fact_reobserved(
        fact_id=fact_id, source_id=source_id, artifact_id=artifact_id
    )

    assert store.get_fact(fact_id)["stale"] == 0
    assert store.get_fact(fact_id)["status"] == "superseded"


def test_updated_at_stays_writable_on_a_superseded_fact(tmp_path):
    # Same carve-out, second column: bookkeeping is not content.
    store = _store(tmp_path)
    fact_id = _rejected_fact(store)

    store._conn.execute(
        "UPDATE facts SET updated_at = '2030-01-01 00:00:00' WHERE id = ?",
        (fact_id,),
    )

    assert store.get_fact(fact_id)["updated_at"] == "2030-01-01 00:00:00"


# --- terminality is not tiering (justinjoy, #310/#311) ---------------------


def test_superseded_belongs_to_neither_status_tier(tmp_path):
    # Hard dependency flagged on both issues. The #287 resolution path — reject a
    # rival, it supersedes, the survivor auto-promotes on the next cascade —
    # works only because superseded sits outside both tiers. Reclassifying it
    # into either one would leave a rejected rival blocking the survivor forever
    # and starve auto-accept.
    #
    # These triggers encode *terminality*, a different axis, and must never be
    # read as licence to move superseded into a tier. This assertion is here so
    # that a change which does gets caught in the same file that might tempt it;
    # the behavioural pin is
    # tests/test_acceptance.py::test_rejecting_one_rival_unblocks_auto_accept_of_the_other.
    assert "superseded" not in ENGINE_STATUSES
    assert "superseded" not in REVIEW_STATUSES


# --- the web routes agree with the store (#311) ----------------------------


def _client(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    client = TestClient(create_app(cfg))
    return client, client.app.state.store


def test_edit_route_hands_back_a_read_only_row_for_a_superseded_fact(tmp_path):
    # fact_row.html has always hidden the edit control on a superseded row, but
    # the control being absent from one render is not the same as the route
    # refusing. Anyone holding the URL — or a page opened before the reject
    # landed — could still pull the form. The route now answers with the row.
    client, store = _client(tmp_path)
    fact_id = _rejected_fact(store)

    resp = client.get(f"/facts/{fact_id}/edit")

    assert resp.status_code == 200
    # The distinguishing mark of the edit partial is the amend form; the row
    # says why there is nothing to do instead.
    assert f'hx-post="/facts/{fact_id}/amend"' not in resp.text
    assert "rejected" in resp.text


def test_edit_route_still_serves_a_form_for_a_live_fact(tmp_path):
    # Over-fix guard for the route above.
    client, store = _client(tmp_path)
    fact_id = store.add_fact("Ledger", "owner", "Park", status="needs_review")

    resp = client.get(f"/facts/{fact_id}/edit")

    assert resp.status_code == 200
    assert f'hx-post="/facts/{fact_id}/amend"' in resp.text


def test_amend_route_on_a_superseded_fact_answers_without_a_server_error(tmp_path):
    # The store raises TerminalFactError, which is a ValueError; unhandled it
    # would be a 500. Reachable from a form that was already open when someone
    # else rejected the fact, so it has to answer cleanly.
    #
    # 200 rather than 4xx on purpose: htmx's default responseHandling does not
    # swap 4xx, so an error status would leave the stale form on screen still
    # offering a save that cannot succeed. Asserting the swapped-in row is what
    # makes this a guard on the user-visible result and not just on the status
    # code.
    client, store = _client(tmp_path)
    fact_id = _rejected_fact(store)
    before = _row(store, fact_id)

    resp = client.post(
        f"/facts/{fact_id}/amend",
        data={"subject": "Ledger", "relation": "owner", "object": "Choi"},
    )

    assert resp.status_code == 200
    assert "rejected" in resp.text
    assert _row(store, fact_id) == before


# --- legacy databases ------------------------------------------------------


def test_a_legacy_db_without_term_token_gets_both_triggers(tmp_path):
    # SQLite resolves a trigger's column references when it fires, not when it is
    # created. So a content trigger naming term_token, if it were defined in
    # schema.sql (which runs before the ALTER that adds the column), would create
    # cleanly on a legacy DB and then abort every later UPDATE with a bare
    # "no such column: NEW.term_token" — including writes to unrelated columns.
    # This builds that DB shape and checks both triggers work rather than
    # misfire.
    db_path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE facts (
            id         INTEGER PRIMARY KEY,
            subject    TEXT NOT NULL,
            relation   TEXT NOT NULL,
            object     TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'candidate',
            confidence REAL NOT NULL DEFAULT 0.0,
            source_id  INTEGER,
            run_id     INTEGER,
            note       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO facts (id, subject, relation, object, status)
        VALUES (1, 'Ledger', 'owner', 'Park', 'superseded');
        """
    )
    legacy.commit()
    legacy.close()

    store = Store(db_path)
    store.init_schema()

    # The bookkeeping carve-out still works — i.e. the content trigger is not
    # aborting every write with a missing-column error.
    store._conn.execute("UPDATE facts SET stale = 1 WHERE id = 1")
    assert store.get_fact(1)["stale"] == 1

    # And both invariants hold on the migrated row.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE facts SET status = 'confirmed' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE facts SET subject = 'Rewritten' WHERE id = 1")
    assert store.get_fact(1)["status"] == "superseded"
