# SPDX-License-Identifier: MPL-2.0
"""Call-time accessors for the fact status tiers.

`verinote.store.db` owns the tier constants, but owning them is not enough: a
consumer that writes `from verinote.store import ENGINE_STATUSES` binds the
frozenset *object* at import time, so widening the tier later moves the Store
(which resolves its own module global on every call) while leaving that
consumer on the old tier. That is exactly the split this package exists to
prevent — the deterministic engine would read facts that Ask, trust and the
planner's schema hint never see.

So the raw frozensets are no longer re-exported from `verinote.store`. Every
status-tier question goes through the accessors here, which resolve
`db.ENGINE_STATUSES` / `db.REVIEW_STATUSES` at call time. One question, one
answer, one runtime definition.

The empty tier is refused here too, for the same reason `_status_filter`
refuses it: an empty tier turns every membership test into a silent "no" —
coverage would call every source a gap, acceptance would promote nothing while
reporting success. A crash is cheaper than a quiet wrong answer.
"""

from __future__ import annotations

from verinote.store import db as _db


def _require_populated(statuses: frozenset[str], name: str) -> frozenset[str]:
    if not statuses:
        raise ValueError(f"{name} status tier must not be empty")
    return statuses


def engine_statuses() -> frozenset[str]:
    """The engine-input tier, read at call time."""
    return _require_populated(_db.ENGINE_STATUSES, "engine-input")


def review_statuses() -> frozenset[str]:
    """The human-review tier, read at call time."""
    return _require_populated(_db.REVIEW_STATUSES, "review")


def is_engine_input(status: object) -> bool:
    """Whether `status` is read by the deterministic engine, decided at call time."""
    return str(status) in engine_statuses()


def is_review_eligible(status: object) -> bool:
    """Whether `status` sits in the human-review tier, decided at call time."""
    return str(status) in review_statuses()
