# SPDX-License-Identifier: MPL-2.0
"""SQLite system-of-record for verinote."""

from verinote.store.db import (
    DEFAULT_REVIEW_PAGE_SIZE,
    FactDecision,
    POLICY_MARKER_KEY,
    REVIEW_PAGE_SIZES,
    ReviewQueuePage,
    Store,
)
from verinote.store.tiers import (
    engine_statuses,
    is_engine_input,
    is_review_eligible,
    review_statuses,
)

# The raw `ENGINE_STATUSES` / `REVIEW_STATUSES` frozensets are deliberately not
# re-exported: importing them binds the tier at import time, which is how the
# engine and the Ask/trust/planner layers drifted onto different tiers. Ask the
# accessors above instead — they resolve `db.ENGINE_STATUSES` at call time.
__all__ = [
    "Store",
    "FactDecision",
    "POLICY_MARKER_KEY",
    "engine_statuses",
    "review_statuses",
    "is_engine_input",
    "is_review_eligible",
    "DEFAULT_REVIEW_PAGE_SIZE",
    "REVIEW_PAGE_SIZES",
    "ReviewQueuePage",
]
