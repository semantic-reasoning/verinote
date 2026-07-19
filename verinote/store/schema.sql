-- SPDX-License-Identifier: MPL-2.0
-- verinote metadata system-of-record (SQLite, OLTP). DuckDB owns canonical
-- logical fact terms in <kb>/facts.duckdb, while verification selects
-- confirmed/accepted fact ids from this file. Analytics attach this file
-- read-only for metadata aggregates.

PRAGMA journal_mode = WAL;        -- concurrent readers + a single writer
PRAGMA foreign_keys = ON;

-- KB-level declarations. Small key/value facts the KB states about itself, so
-- code never has to *infer* them. `policy.logic` records that this KB has a
-- logic policy file (sha256 + timestamp + origin as evidence only — the .dl
-- file owns the policy text), which is what lets a later disappearance of that
-- file be reported as an error instead of a benign "no rules" default.
-- init_schema() runs this script on every open, so CREATE TABLE IF NOT EXISTS
-- is also the migration for existing KBs.
CREATE TABLE IF NOT EXISTS kb_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A source document the facts are cited against. For binary uploads this is the
-- original file, not the converted text used for extraction.
CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,      -- relative path under the KB root
    kind       TEXT NOT NULL DEFAULT 'text',  -- text | binary
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Text artifacts used by extraction. Text sources point at their original file;
-- binary sources point at a derived UTF-8 text artifact.
CREATE TABLE IF NOT EXISTS source_artifacts (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('original_text','extracted_text')),
    path         TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    checksum     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, kind, checksum)
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
    artifact_id      INTEGER REFERENCES source_artifacts(id) ON DELETE SET NULL,
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
    job_id     INTEGER REFERENCES extraction_jobs(id) ON DELETE SET NULL,
    note       TEXT NOT NULL DEFAULT '',
    term_token TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_review_status_id ON facts(status, id);
CREATE INDEX IF NOT EXISTS idx_facts_triple ON facts(subject, relation, object);

-- Source-backed evidence anchors for extracted facts. Chunk-level anchors are
-- available for every chunked extraction; exact spans/table cells can be added
-- later without changing the fact contract.
CREATE TABLE IF NOT EXISTS fact_evidence (
    id            INTEGER PRIMARY KEY,
    fact_id       INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    artifact_id   INTEGER REFERENCES source_artifacts(id) ON DELETE SET NULL,
    job_id        INTEGER REFERENCES extraction_jobs(id) ON DELETE SET NULL,
    chunk_id      INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    evidence_kind TEXT NOT NULL DEFAULT 'chunk',
    start_offset  INTEGER,
    end_offset    INTEGER,
    locator       TEXT NOT NULL DEFAULT '',
    snippet       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (length(evidence_kind) > 0),
    CHECK (start_offset IS NULL OR start_offset >= 0),
    CHECK (end_offset IS NULL OR end_offset >= 0),
    CHECK (
        start_offset IS NULL OR end_offset IS NULL OR end_offset >= start_offset
    )
);

CREATE INDEX IF NOT EXISTS idx_fact_evidence_fact ON fact_evidence(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_evidence_source ON fact_evidence(source_id);
CREATE INDEX IF NOT EXISTS idx_fact_evidence_chunk ON fact_evidence(chunk_id);

-- Natural-language questions translated to a Datalog query draft (#3). status:
-- pending -> translated (an `answer_q<id>` rule) | review_required/no_answer/
-- translation_failed/ambiguous (visible non-executable outcomes).
CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    query_dl   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending'
                 CHECK (
                     status IN (
                         'pending',
                         'translated',
                         'review_required',
                         'translation_failed',
                         'no_answer',
                         'ambiguous'
                     )
                 ),
    reason     TEXT NOT NULL DEFAULT '',
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

-- Append-only lifecycle events for facts. Unlike review_log, this records
-- system-origin events such as extraction and chunk processing as well as human
-- review actions. Payloads carry metadata only, not source document bodies.
CREATE TABLE IF NOT EXISTS fact_events (
    id          INTEGER PRIMARY KEY,
    fact_id     INTEGER REFERENCES facts(id) ON DELETE SET NULL,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    job_id      INTEGER REFERENCES extraction_jobs(id) ON DELETE SET NULL,
    chunk_id    INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    rule_name   TEXT NOT NULL DEFAULT '',
    before_json TEXT,
    after_json  TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (length(event_type) > 0),
    CHECK (actor IN ('system','human','rule'))
);

CREATE INDEX IF NOT EXISTS idx_fact_events_fact ON fact_events(fact_id, id);
CREATE INDEX IF NOT EXISTS idx_fact_events_job ON fact_events(job_id);
