# SPDX-License-Identifier: MPL-2.0
"""The Sources page counts a claimed chunk as running, not pending (#475).

DOMAIN NOTE — this is deliberately tested on a LIVE job, and that is a different
domain from the rest of #475's tests. Those pin that a chunk is never left
`running` once a job pass ends; with that fixed, a job in a terminal status can
no longer have a `running` chunk to display, so the display can only be exercised
while a job is genuinely in flight. The reported symptom (a `failed` job showing
"49 pending" for 48 untouched chunks plus one claimed) is the same miscount seen
in a state that no longer occurs.

What is asserted is the rendered page, through `create_app` + `TestClient`, so
the `_source_inspector_rows` -> `sources.html` path is the thing under test
rather than a hand-built context dict.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.web import create_app  # noqa: E402


def test_an_in_flight_job_shows_its_claimed_chunk_apart_from_the_waiting_ones(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    store = app.state.store
    source_id = store.add_source("sources/a.txt")
    job_id = store.create_extraction_job(
        source_id=source_id, provider="anthropic", model="m", total_chunks=4
    )
    chunk_ids = store.add_source_chunks(
        job_id=job_id, source_id=source_id, chunks=["a", "b", "c", "d"]
    )
    # A job mid-pass: one chunk finished, one claimed and out at the LLM, two still
    # in the queue.
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_done(chunk_ids[0], candidates=1)
    assert store.mark_chunk_running(chunk_ids[1]) is not None

    html = TestClient(app).get("/sources").text

    assert "1 running" in html
    assert "2 pending" in html
    # the miscount: 2 waiting + 1 claimed reported as three chunks nobody has touched
    assert "3 pending" not in html
