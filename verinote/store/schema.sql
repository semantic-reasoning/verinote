-- SPDX-License-Identifier: Apache-2.0
-- verinote system-of-record (SQLite, OLTP). DuckDB attaches this file read-only
-- for analytics. The wirelog `.dl` engine input is DERIVED from confirmed rows;
-- it is never the source of truth.

PRAGMA journal_mode = WAL;        -- concurrent readers + a single writer
PRAGMA foreign_keys = ON;

-- A source document the facts are extracted from and cited against.
CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,      -- relative path under the KB root
    kind       TEXT NOT NULL DEFAULT 'text',  -- text | conversion | binary
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One extraction run (an LLM pass over sources). Facts cite the run that
-- produced them, so a run can be inspected or retired as a unit.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    provider   TEXT,
    model      TEXT,
    summary    TEXT
);

-- A candidate/verified fact: a (subject, relation, object) triple with status.
-- Status lifecycle:  candidate -> needs_review -> confirmed -> accepted
--                                              \-> superseded (retired)
-- Only confirmed/accepted rows compile into the wirelog `.dl` engine input.
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    relation   TEXT NOT NULL,
    object     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'candidate'
                 CHECK (status IN ('candidate','needs_review','confirmed','accepted','superseded')),
    confidence REAL NOT NULL DEFAULT 0.0,
    source_id  INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_triple ON facts(subject, relation, object);

-- Append-only audit of human review decisions (toggle/accept/reject/amend),
-- mirroring the "decisions are preserved" property of the borrowed concept.
CREATE TABLE IF NOT EXISTS review_log (
    id          INTEGER PRIMARY KEY,
    fact_id     INTEGER REFERENCES facts(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,            -- toggled | accepted | rejected | amended | deleted
    before_json TEXT,
    after_json  TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
