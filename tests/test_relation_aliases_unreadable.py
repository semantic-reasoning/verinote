# SPDX-License-Identifier: MPL-2.0
"""An unreadable `policy/relation-aliases.md` ends the job, and names itself (#553).

THE PROPERTY IS THE JOB'S, NOT THE EXCEPTION'S. "an `LLMError` is raised" would
pass against a helper that raised one into a caller that still stranded the job,
so these tests drive a real `process_extraction_job` and read the job row back --
and all but the `malformed` one read its `fact_events` too. What #553 costs an
operator is a job stuck at `running`, not an exception type: no plain `verinote
sync` rewinds it -- every later pass skips it as `busy_job_id` -- and only
`verinote sync --recover` or a `verinote ui` boot gets it moving again, the pair
`cli.py`'s own skip message names.

WHY THE MODES ARE NOT REDUNDANT. `non_utf8` is the reported reproduction and the
only one that pins the MESSAGE from its own text: `str(UnicodeDecodeError)` names
no file, so without the clause under test the chunk's error says nothing about
which file. The error assertion is on the whole prefix rather than a substring of
it because `str(PermissionError)` already carries the path: under a substring
assertion, a clause that caught the failure but dropped the file name would still
pass in the `chmod` mode.

`chmod` pins ONE HALF of the clause's breadth, and no more. `PermissionError` is
not a `ValueError`, so a clause narrowed to `UnicodeDecodeError` -- or to
anything else under `ValueError` -- strands the job again on it. That is the
whole of what it pins: `except OSError` is a single named type and catches
`PermissionError`, and `except (UnicodeDecodeError, PermissionError)` leaves both
modes green. What pins the clause as BROAD rather than a list of types is
`test_an_unlisted_alias_read_failure_still_ends_the_job` below.

`malformed` is here to pin the clause's PLACEMENT. A readable file with a syntax
error already gets a message from `relation_aliases` that begins with the file
name; if the new broad clause were moved above the `CorroborationPolicyError`
one, that message would arrive wearing a second copy of the file name.
"""

from contextlib import contextmanager
from pathlib import Path

import pytest

from verinote.llm.base import ExtractedFact
from verinote.pipeline.extract import (
    create_chunked_extraction_job,
    process_extraction_job,
)
from verinote.policy_defaults import RELATION_ALIASES_RELPATH
from verinote.store import Store

SOURCE_TEXT = "alpha"
NAMED = f"{RELATION_ALIASES_RELPATH} could not be read: "


class _Client:
    """One fact for the one chunk.

    The alias read sits AFTER the LLM call in `_extract_chunk`, so a client that
    failed would never reach the code under test.
    """

    name = "stub"

    def extract_facts(self, *, source_text: str, schema_hint: str = ""):
        return [ExtractedFact(source_text.strip(), "seen_in", "source", 0.9)]


def _one_chunk_job(tmp_path: Path) -> tuple[Store, int]:
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("sources/a.txt")
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=SOURCE_TEXT,
        provider="fake",
        model="m",
        chunk_chars=64,
        chunk_overlap_chars=0,
    )
    return store, job_id


@contextmanager
def _broken_alias_file(tmp_path: Path, mode: str):
    """Make the alias file unreadable, the two ways these tests use.

    A context manager, and entered BEFORE the `Store` is opened, so that the
    `chmod` mode's runtime skip -- which raises out of this function -- happens
    while no connection and no job exist yet. The skip is a runtime one after an
    actual read attempt rather than a `geteuid` marker, so it also covers a
    filesystem that ignores the mode bit; that shape and the `exists()` guard
    below are `tests/test_pipeline.py::_broken_override`'s.
    """
    path = tmp_path / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "non_utf8":
        path.write_bytes("소속 -> `member_of`\n".encode("cp949"))
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
        return
    path.write_text("- 소속 -> `member_of`\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")
        yield
    finally:
        # `pytest.skip` above and a failed assertion in the body both leave
        # through here, so restoring the mode and removing the file on this one
        # path is what keeps a mode-0o000 file out of the retained tmp dir on
        # every exit.
        if path.exists():
            path.chmod(0o600)
            path.unlink()


def _event_types(store: Store, job_id: int) -> list[str]:
    return [
        row["event_type"]
        for row in store._conn.execute(
            "SELECT event_type FROM fact_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
    ]


@pytest.mark.parametrize("mode", ["non_utf8", "chmod"])
def test_an_unreadable_alias_file_ends_the_job_and_names_itself(tmp_path, mode):
    with _broken_alias_file(tmp_path, mode):
        store, job_id = _one_chunk_job(tmp_path)
        try:
            # Nothing escapes: the loop's broad clause re-raises a non-`LLMError`,
            # and that raise is what left the job `running`.
            process_extraction_job(store, _Client(), job_id=job_id)

            job = store.get_extraction_job(job_id)
            assert job["status"] == "failed"
            assert "extraction_job_completed" in _event_types(store, job_id)

            chunks = store.source_chunks(job_id)
            assert [c["status"] for c in chunks] == ["failed"]
            assert str(chunks[0]["error"]).startswith(NAMED)
        finally:
            store.close()


class _Unlisted(Exception):
    """A failure nobody enumerated: neither a `ValueError` nor an `OSError`."""


def test_an_unlisted_alias_read_failure_still_ends_the_job(tmp_path, monkeypatch):
    """The pin that does not depend on a list of types.

    The two failures the tests above reach through a real file are examples, not
    the set. This repo has refused that same pair as complete before: see
    `tests/test_cloud_adapters.py::test_a_render_failure_of_a_kind_nobody_enumerated_is_still_an_llm_error`,
    whose docstring records #500's reviewer doing it. Narrow the clause to
    `except (UnicodeDecodeError, PermissionError)` and both tests above stay
    green while this one fails.

    No file is involved: `store_relation_aliases` is replaced outright, so this
    reaches the clause with a type no read can produce.
    """
    store, job_id = _one_chunk_job(tmp_path)

    def boom(*args, **kwargs):
        raise _Unlisted("nobody enumerated this")

    monkeypatch.setattr("verinote.pipeline.extract.store_relation_aliases", boom)
    try:
        process_extraction_job(store, _Client(), job_id=job_id)

        assert store.get_extraction_job(job_id)["status"] == "failed"
        assert "extraction_job_completed" in _event_types(store, job_id)

        chunks = store.source_chunks(job_id)
        assert [c["status"] for c in chunks] == ["failed"]
        assert str(chunks[0]["error"]).startswith(NAMED)
    finally:
        store.close()


def test_a_malformed_alias_file_keeps_its_own_message(tmp_path):
    """The narrow clause still runs first, so its message is not re-wrapped."""
    path = tmp_path / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- 소속 member_of\n", encoding="utf-8")
    store, job_id = _one_chunk_job(tmp_path)
    try:
        process_extraction_job(store, _Client(), job_id=job_id)

        assert store.get_extraction_job(job_id)["status"] == "failed"
        error = str(store.source_chunks(job_id)[0]["error"])
        assert error.startswith("relation-aliases.md:1: expected")
        assert not error.startswith(NAMED)
    finally:
        store.close()
