# SPDX-License-Identifier: MPL-2.0
"""Materialise a Google Form sheet's response batch as a verinote source (#478).

This is the *first arrow* of Form Sync (#476–#481): the one part that is genuinely
new. Everything downstream — ``store_source`` → ``create_chunked_extraction_job`` →
chunking → LLM extraction → candidate facts → the human review gate, ``fact_evidence``,
trust labels, corroboration, ``/report`` provenance, the dashboard coverage table —
already exists and does not care whether the bytes came from a file upload or a sheet.
Feeding the batch through that pipe is what keeps form responses first-class citizens
instead of a second-tier path that the verification machine (the reason this product
exists) cannot see.

What this module deliberately is NOT (the split from #479, the worker):

* A scheduler. It fetches one batch, materialises it, and returns. Deciding *when*
  to check, the ``next_check_at`` backoff, and the error→``last_error_kind`` mapping
  are #479's job. This module hands #479 exactly the inputs it needs: the new
  watermark, the rotated refresh token to persist, and (on failure) the
  ``GoogleSheetsError`` with its ``status``/``retryable``/``live_refresh_token``.
* A transport owner. The dependency policy, timeouts, quota behaviour, and the
  ``_open``/``_sleep`` test seam are #486's ``verinote/google_sheets.py``. This
  module receives an *injectable* ``read`` (default ``read_sheet_values``) so the
  base suite never dials Google.

The two design forks #478 had to settle, and the answers:

* **A batch IS a source, not the form.** Every fetch materialises one *new,
  immutable* ``sources/form-<sheet>-<stamp>.txt`` row. The alternative (one source
  row per form, updated in place) collides with ``POST /sources/{id}/reanalyze``:
  re-extracting a form's whole accumulated text would re-candidate responses a
  human already judged. An immutable batch has no such conflict — re-analysing a
  batch re-candidates only that batch. The cost is a coverage-table row per batch,
  which is accepted: it is also the audit trail of *when* responses arrived.
* **The watermark is a position — "responses submitted at or before this row" —
  not a timestamp.** It is the count of response rows (the sheet's data rows
  excluding the header) read so far, stored as a decimal string in
  ``form_sources.watermark``. Position-based is locale-proof (a ``Timestamp`` column
  is a localised string whose ordering cannot be trusted across month/day/format
  boundaries) and trivially deterministic to test. The consequence #478 records as
  a known limitation (paired with #477's): **edited responses are not re-fetched** —
  a response's text may change in place without its row position changing, and
  Google Forms exposes no "modified at" field in the response sheet to key on.
  Supporting "modified after" would mean reading a different field; that is deferred.

Reading strategy. ``fetch_form_responses`` reads the whole first tab (``A1:ZZ`` by
default: the header row plus every response row) and slices *locally* at the
watermark. This is deliberate against #486's contract: the client treats a range
that returns no ``values`` array as a *definitive shape error*, and "no new
responses" is the normal steady state (the most common outcome of a check), so a
watermark-anchored range would turn the healthiest case into an error. Reading the
header + all rows and filtering means (a) the header is present for question labels,
(b) "no new responses" is an empty slice, not an error, and (c) already-read
responses are *never re-materialised* — only rows past the watermark reach
``store_source``. The cost of re-reading old rows is a transport/scheduling concern
for #479's cadence, not a correctness one here.

Rotation contract inherited from #486: on success the latest rotated refresh token
ridges ``ResponseBatch.rotated_refresh_token`` (``None`` when nothing rotated); on
failure the client raises carrying ``live_refresh_token``. The caller's duty is
unchanged: persist it via ``save_google_grant`` before anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from verinote.config import GoogleGrant
from verinote.google_sheets import SheetReadResult, read_sheet_values
from verinote.pipeline.extract import create_chunked_extraction_job
from verinote.pipeline.ingest import IngestError, store_source
from verinote.store import Store
from verinote.text import nfc

# The injected read seam: the exact signature of ``google_sheets.read_sheet_values``.
ReadFn = Callable[[GoogleGrant, str, str, str], SheetReadResult]

# A1 range covering the first tab: header row + every response row. ``ZZ`` is
# column 702, so a real form's question columns are never silently truncated — and
# note that ``A1`` alone would be ONE cell (the header's first label), which is
# why the range is a range (Sheets trims trailing empty columns, so the width is
# not a cost). Overridable for a specific layout.
DEFAULT_VALUE_RANGE = "A1:ZZ"

# sheet_id is already constrained to ``[A-Za-z0-9_-]{1,64}`` by #486's validation; this
# is only the filename-safe defensive re-statement for the citation name.
_UNSAFE_SHEET_ID = re.compile(r"[^A-Za-z0-9_-]")

# A response sheet's first column carries the submission time under this label.
_TIMESTAMP_LABEL = "timestamp"


@dataclass(frozen=True)
class ResponseBatch:
    """One fetch of a form sheet, sliced to the responses *new* since the watermark.

    ``responses`` holds only rows past ``since_watermark`` — the materialisable unit.
    ``header`` is the sheet's label row (first cell is usually ``Timestamp``).
    ``new_watermark`` is the position to record after this batch (see #477's
    ``form_sources.watermark``). ``rotated_refresh_token`` is ``None`` unless Google
    rotated the grant during the read (#486's rotation contract).
    """

    sheet_id: str
    header: tuple[str, ...]
    responses: tuple[tuple[str, ...], ...]
    new_watermark: str
    rotated_refresh_token: str | None = field(default=None, repr=False)


def parse_watermark(watermark: str | None) -> int:
    """``form_sources.watermark`` → the count of response rows already read.

    ``None``/``""`` mean "never read" → 0. A watermark is a non-negative integer
    position; anything else is a corrupt state and is refused loudly rather than
    silently misread as 0 (which would re-materialise an entire form).
    """
    if watermark is None or watermark == "":
        return 0
    if isinstance(watermark, bool) or not isinstance(watermark, str):
        raise ValueError(f"watermark must be a decimal string, got {type(watermark).__name__}")
    if not watermark.strip().isdigit():
        raise ValueError(f"invalid watermark (expected a non-negative integer): {watermark!r}")
    value = int(watermark)
    if value < 0:
        raise ValueError(f"watermark must be >= 0, got {value}")
    return value


def fetch_form_responses(
    grant: GoogleGrant,
    client_id: str,
    sheet_id: str,
    *,
    since_watermark: str | None,
    value_range: str = DEFAULT_VALUE_RANGE,
    read: ReadFn = read_sheet_values,
) -> ResponseBatch:
    """Read one form sheet and return the responses new since ``since_watermark``.

    Reads the whole first tab (header + all response rows) via the injectable
    ``read`` (default the #486 client) and slices at the watermark locally. The
    seam exists at BOTH layers — this function and ``materialize_form_batch`` —
    with the client's exact signature
    ``(grant, client_id, sheet_id, value_range) -> SheetReadResult``, so the base
    suite and #479 never dial Google. The client's own errors —
    ``TokenRefreshError``, ``SheetReadError`` with
    ``status``/``retryable``/``live_refresh_token`` — propagate unchanged so the
    caller (#479) can persist a rotated token and map the failure.

    ``since_watermark`` is required by contract: it is what keeps already-reviewed
    responses from being re-materialised on every check.
    """
    result = read(grant, client_id, sheet_id, value_range)
    rows = result.rows
    if not rows:
        # The client guarantees a ``values`` array on success, so an empty tuple is a
        # degenerate (non-form) sheet, not an error we must invent. Treat as "no
        # responses yet": a zero batch the caller records and moves on from.
        return ResponseBatch(
            sheet_id=sheet_id,
            header=(),
            responses=(),
            new_watermark="0",
            rotated_refresh_token=result.rotated_refresh_token,
        )
    header = rows[0]
    all_responses = rows[1:]
    start = parse_watermark(since_watermark)
    # A watermark past the end (rows deleted out from under us) clamps to "nothing
    # new" instead of a negative slice re-reading from the top.
    if start > len(all_responses):
        start = len(all_responses)
    new_responses = all_responses[start:]
    # Deliberately NON-monotonic: if the sheet shrank (5 rows read, now 3), the
    # watermark moves BACK to "3" and the next new row (4) IS read. A "max
    # watermark" would clamp to 5 and skip row 4 forever — the [H] fix the
    # Critic's scenario proved to be a permanent silent ingestion stop. The
    # shrink IS the resume mechanism.
    return ResponseBatch(
        sheet_id=sheet_id,
        header=header,
        responses=new_responses,
        new_watermark=str(len(all_responses)),
        rotated_refresh_token=result.rotated_refresh_token,
    )


def render_responses_as_text(batch: ResponseBatch) -> str:
    """Render a batch's new responses as extraction-ready text.

    One line per response: an optional ``submitted <ts>`` context (when the first
    column is the form's ``Timestamp``) followed by ``label=answer`` pairs. A deleted
    question (empty header label) is named by its position; a skipped answer renders
    as ``(no answer)``; extra cells beyond the header are labelled by column. An empty
    batch renders as the empty string, which is the caller's signal to materialise
    nothing. Sanitising NULs and NFC-normalising is ``store_source``'s single job
    (#473), so this function does not duplicate it.
    """
    if not batch.responses:
        return ""
    header = batch.header
    ts_index = 0 if (header and header[0].strip().lower() == _TIMESTAMP_LABEL) else None
    lines: list[str] = []
    for index, row in enumerate(batch.responses, start=1):
        parts: list[str] = []
        for col, label in enumerate(header):
            answer = (row[col] if col < len(row) else "").strip()
            if col == ts_index:
                if answer:
                    parts.append(f"submitted {answer}")
                continue
            if not (label or "").strip():
                label = f"question {col + 1}"
            parts.append(f"{label.strip()}={answer or '(no answer)'}")
        for col in range(len(header), len(row)):
            answer = (row[col] or "").strip()
            parts.append(f"(column {col + 1})={answer or '(no answer)'}")
        lines.append(f"[response {index}] " + "; ".join(parts))
    return "\n".join(lines) + "\n"


def _utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_sheet_id(sheet_id: str) -> str:
    return _UNSAFE_SHEET_ID.sub("_", sheet_id) or "sheet"


def materialize_form_batch(
    store: Store,
    root: Path,
    grant: GoogleGrant,
    client_id: str,
    sheet_id: str,
    *,
    since_watermark: str | None,
    provider: str | None = None,
    model: str | None = None,
    chunk_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
    value_range: str = DEFAULT_VALUE_RANGE,
    read: ReadFn = read_sheet_values,
    stamp: str | None = None,
) -> dict:
    """Fetch a batch, materialise it as a source, and queue its extraction job.

    This is #478's "first arrow": ``fetch_form_responses`` →
    ``render_responses_as_text`` → ``store_source`` → ``create_chunked_extraction_job``.
    The worker that calls this on a schedule (#479) then records the check with the
    returned ``new_watermark``, persists the returned ``rotated_refresh_token`` if not
    ``None``, and starts the extraction.

    An empty batch (nothing new since the watermark) materialises *nothing* — no
    source row, no job — and returns ``materialized=False`` with the (unchanged)
    watermark, so the caller can still record the check and persist a rotated token.

    ``stamp`` is the batch's identity suffix in
    ``sources/form-<sheet>-<stamp>.txt``; production leaves it to the UTC clock
    (second granularity) and tests inject a fixed value for determinism.

    Collision rejection: ``Store.add_source`` upserts on the citation path, so a
    second batch landing on the same citation (same sheet + stamp — e.g. two
    checks within one clock second) would SILENTLY merge: the second batch's raw
    bytes overwrite the first file under the first source row, whose candidates
    a human has already judged. That is data loss wearing the costume of
    idempotency, so it is refused with ``IngestError`` BEFORE any side effect
    (no file, no row, no artifact, no job); the caller records a failed check and
    the unadvanced watermark guarantees the next check re-reads the same rows.
    The one accepted residue window sits the other way: a failure BETWEEN
    ``store_source`` and job creation leaves a source row with no job — a valid,
    inspectable KB object (re-analysable), i.e. an auditable orphan, not
    corruption.
    """
    batch = fetch_form_responses(
        grant,
        client_id,
        sheet_id,
        since_watermark=since_watermark,
        value_range=value_range,
        read=read,
    )
    base: dict = {
        "sheet_id": sheet_id,
        "new_watermark": batch.new_watermark,
        "rotated_refresh_token": batch.rotated_refresh_token,
        "response_count": len(batch.responses),
    }
    if not batch.responses:
        return {
            **base,
            "materialized": False,
            "citation": None,
            "source_id": None,
            "artifact_id": None,
            "job_id": None,
            "unreadable_chars": None,
        }

    text = render_responses_as_text(batch)
    filename = f"form-{_safe_sheet_id(sheet_id)}-{stamp or _utcstamp()}.txt"
    # Collision guard (adjudicated [H]): refuse a second batch on the same
    # citation before ANY side effect — `Store.add_source` upserts on path, so
    # without this the second batch would overwrite the first batch's raw file
    # under a source row whose candidates may already be human-judged. The check
    # lives here rather than in `store_source` because upsert-by-path is the
    # LEGITIMATE behaviour of the shared upload path (re-registering a file).
    name = nfc(Path(filename).name)
    citation = f"sources/{name}"
    if store.get_source_by_path(citation) is not None:
        raise IngestError(
            f"a form batch already exists at {citation!r}; refusing to merge a "
            "second batch into the same source row (same sheet and stamp)"
        )
    stored = store_source(store, Path(root), filename, text.encode("utf-8"), text, "text")
    job_id = create_chunked_extraction_job(
        store,
        source_id=stored["source_id"],
        artifact_id=stored["artifact_id"],
        source_text=stored["text"],
        provider=provider,
        model=model,
        chunk_chars=chunk_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )
    return {**base, "materialized": True, **stored, "job_id": job_id}
