# SPDX-License-Identifier: MPL-2.0
"""The `form_sources` table and its `Store` accessors (#477).

Form Sync persists a registered sheet per KB. The screen answers three
questions per form — last checked / next check / any problem — and the first
must survive a restart, hence a table. The load-bearing invariant is the split
between `watermark` ("where we read up to") and `last_checked_at` ("when we
last looked"): a failed check advances the timestamp but must not advance the
watermark. A second, screen-facing invariant is that `last_error_kind` is a
closed behavior classification, never an HTTP status or exception name.
"""

import sqlite3

import pytest

from verinote.store.db import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_crud_round_trip(tmp_path):
    s = _store(tmp_path)
    fid = s.add_form_source("sheet_abc", "Sample Form")
    row = s.get_form_source(fid)
    assert row["sheet_id"] == "sheet_abc"
    assert row["name"] == "Sample Form"
    assert row["enabled"] == 1
    assert row["watermark"] is None
    assert row["last_checked_at"] is None
    assert row["last_error_kind"] is None
    assert row["created_at"] is not None

    # lookup by sheet_id
    assert s.get_form_source_by_sheet_id("sheet_abc")["id"] == fid
    assert s.get_form_source_by_sheet_id("nope") is None

    # list
    assert [r["id"] for r in s.form_sources()] == [fid]

    # disable / re-enable
    s.set_form_enabled(fid, False)
    assert s.get_form_source(fid)["enabled"] == 0
    s.set_form_enabled(fid, True)
    assert s.get_form_source(fid)["enabled"] == 1

    # delete
    s.delete_form_source(fid)
    assert s.get_form_source(fid) is None
    assert s.form_sources() == []


def test_duplicate_sheet_id_is_refused(tmp_path):
    s = _store(tmp_path)
    fid = s.add_form_source("sheet_abc", "Sample Form")
    with pytest.raises(ValueError, match="already registered"):
        s.add_form_source("sheet_abc", "Another Name")
    # the original row is untouched and there is exactly one row
    assert s.get_form_source(fid)["name"] == "Sample Form"
    assert len(s.form_sources()) == 1


def test_failed_check_does_not_advance_watermark(tmp_path):
    s = _store(tmp_path)
    fid = s.add_form_source("sheet_abc", "Sample Form")

    # a healthy check advances the watermark
    s.record_check_result(
        fid, watermark="row_5", last_checked_at="2026-09-26 10:00",
        next_check_at="2026-09-26 16:00", error_kind=None,
    )
    assert s.get_form_source(fid)["watermark"] == "row_5"
    assert s.get_form_source(fid)["last_error_kind"] is None

    # a failed check records the SAME (old) watermark with the error set
    s.record_check_result(
        fid, watermark="row_5", last_checked_at="2026-09-26 17:00",
        next_check_at="2026-09-27 10:00", error_kind="token_expired",
    )
    row = s.get_form_source(fid)
    assert row["watermark"] == "row_5"            # unchanged
    assert row["last_checked_at"] == "2026-09-26 17:00"  # advanced
    assert row["last_error_kind"] == "token_expired"


def test_last_error_kind_accepts_only_the_closed_set(tmp_path):
    s = _store(tmp_path)
    fid = s.add_form_source("sheet_abc", "Sample Form")

    for kind in ("token_expired", "sheet_forbidden", None):
        s.record_check_result(
            fid, watermark="row_1", last_checked_at="t", next_check_at="u", error_kind=kind,
        )
        assert s.get_form_source(fid)["last_error_kind"] == kind

    for bad in ("403", "RefreshError", "HTTP 403", "sheet_error", ""):
        with pytest.raises(ValueError, match="unknown form error kind"):
            s.record_check_result(
                fid, watermark="row_1", last_checked_at="t", next_check_at="u", error_kind=bad,
            )
        # the rejection is pure: nothing was written
        assert s.get_form_source(fid)["last_error_kind"] in (None, "sheet_forbidden")


def test_opening_a_pre_477_kb_migrates_in_the_table(tmp_path):
    # A pre-#477 KB has a `sources` table with data and no `form_sources`.
    # Reopening must add the table without disturbing the existing data.
    conn = sqlite3.connect(tmp_path / "kb.sqlite")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sources ("
        "id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, "
        "kind TEXT NOT NULL DEFAULT 'text', "
        "added_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO sources(path) VALUES('docs/legacy.txt')")
    conn.commit()
    conn.close()

    s = _store(tmp_path)
    # the legacy row survived
    assert [r["path"] for r in s.sources()] == ["docs/legacy.txt"]
    # and the new table is now usable
    fid = s.add_form_source("sheet_abc", "Sample Form")
    assert s.get_form_source(fid)["sheet_id"] == "sheet_abc"
