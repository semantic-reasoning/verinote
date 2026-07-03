-- SPDX-License-Identifier: MPL-2.0
-- verinote metadata system-of-record (SQLite, OLTP). DuckDB owns canonical
-- logical fact terms in <kb>/facts.duckdb, while verification selects
-- confirmed/accepted fact ids from this file. Analytics attach this file
-- read-only for metadata aggregates.

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

-- Durable source-analysis progress. A job owns chunk rows so analysis can
-- resume/retry without depending on an in-memory web process.
CREATE TABLE IF NOT EXISTS extraction_jobs (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','running','done','failed','canceled')),
    provider         TEXT,
    model            TEXT,
    total_chunks     INTEGER NOT NULL DEFAULT 0,
    completed_chunks INTEGER NOT NULL DEFAULT 0,
    failed_chunks    INTEGER NOT NULL DEFAULT 0,
    candidate_count  INTEGER NOT NULL DEFAULT 0,
    message          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    job_id      INTEGER NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','done','failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_extraction_jobs_source ON extraction_jobs(source_id);
CREATE INDEX IF NOT EXISTS idx_source_chunks_job_status ON source_chunks(job_id, status);

-- A candidate/verified fact with review metadata. The subject/relation/object
-- columns are text display mirrors and legacy backfill inputs; the authoritative
-- logical terms live in DuckDB fact_terms keyed by this row id.
-- Status lifecycle:  candidate -> needs_review -> confirmed -> accepted
--                                              \-> superseded (retired)
-- Only confirmed/accepted ids are eligible for deterministic engine input.
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

-- Natural-language questions translated to a Datalog query draft (#3). status:
-- pending -> translated (an `answer_q<id>` rule) | review_required (untranslatable).
CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    query_dl   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','translated','review_required')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
