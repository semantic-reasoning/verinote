# SPDX-License-Identifier: MPL-2.0
"""verinote command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
import sys
from pathlib import Path

from verinote import __version__
from verinote.config import Config, local_root
from verinote.pipeline.question_outcome import format_question_outcome
from verinote.store import Store, engine_statuses

# Local commands own their KB location: they never inherit the KB the web UI
# last selected, so `verinote init` cannot scribble into somebody else's data.
_LOCAL_ROOT_COMMANDS = frozenset({"init", "seed"})

_DEMO_FACTS = [
    # (subject, relation, object, status, confidence, source, note)
    # Obviously-fictional placeholder data: it only demonstrates the status
    # lifecycle and the (subject, relation, object) shape. Do NOT put real
    # organisations, people, or grant references here.
    #
    # No demo fact may carry an engine status (see `ENGINE_STATUSES`): demo data
    # must not become engine input without a human passing it through review.
    # `tests/test_cli.py` enforces that as an invariant.
    ("Example Org", "is_a", "participant", "needs_review", 0.95, "sources/example-grant.txt", "participant"),
    ("Example Org", "established_on", "2020-01-01", "candidate", 0.98, "sources/example-grant.txt", ""),
    ("Demo Project", "has_participant", "Example Org", "candidate", 0.92, "sources/example-grant.txt", ""),
    ("wirelog", "is_a", "deterministic logic engine", "candidate", 0.90, "sources/example-notes.txt", ""),
]


@dataclass(frozen=True)
class _SourceInput:
    source_path: str
    text: str
    source_id: int | None = None
    artifact_id: int | None = None


@dataclass(frozen=True)
class _SyncSummary:
    per_source: list[tuple[str, int]]
    total: int
    run_id: int


def _store(cfg: Config) -> Store:
    from verinote.pipeline.policy_state import ensure_policy_marker

    store = Store(cfg.db_path)
    store.init_schema()
    # Opening a KB is where an existing (pre-marker) policy file gets adopted, so
    # KBs created before markers existed keep working — and any later loss of
    # their policy file is loud rather than silently defaulted.
    ensure_policy_marker(store, cfg.root)
    return store


def _scaffold_policy(cfg: Config, store: Store) -> Path | None:
    """Write the default logic policy only when the KB never had one.

    A KB that recorded a policy whose file is now gone is *not* re-scaffolded:
    rewriting the default there would overwrite the evidence of the loss with a
    plausible-looking green KB. That recovery needs a human (`policy reset
    --force`).
    """
    from verinote.pipeline.policy_state import (
        PolicyMissingError,
        PolicyStatus,
        policy_missing_message,
        resolve_policy,
        write_default_policy,
    )

    state = resolve_policy(store)
    if state.status is PolicyStatus.PRESENT:
        return None
    if state.status is PolicyStatus.MISSING_RECORDED:
        raise PolicyMissingError(policy_missing_message(state))
    return write_default_policy(store, cfg.root, origin="scaffold")


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.pipeline.policy_state import PolicyMissingError

    # `mkdir(exist_ok=True)` still raises when a *file* sits at the root path,
    # so name the problem instead of showing a traceback.
    if cfg.root.exists() and not cfg.root.is_dir():
        print(
            f"cannot create a KB at {cfg.root}: it exists and is not a directory; "
            f"name a different root (`verinote init <path>`)",
            file=sys.stderr,
        )
        return 1
    # A `kb.sqlite` that SQLite cannot read is not an empty slot to scaffold into:
    # `init_schema()` would raise a raw `sqlite3.DatabaseError` at the user. It may
    # also be a real KB with a damaged header — every review decision they ever
    # made — so overwriting it is not ours to choose. Refuse, and say what to do.
    # A readable database with no schema is the opposite case: filling it in is
    # exactly what `init` is for, so that one is left to proceed.
    if cfg.db_path.is_file() and _kb_schema_problem(cfg.db_path) == KB_UNREADABLE:
        print(
            f"cannot initialise {cfg.root}: {cfg.db_path} exists and is {KB_UNREADABLE}. "
            "If it is a damaged KB, restore it from backup; if it is not a KB at all, "
            "move it aside and run this again.",
            file=sys.stderr,
        )
        return 1
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = _store(cfg)
    if args.seed:
        _seed(store)
    try:
        policy = _scaffold_policy(cfg, store)
    except PolicyMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        store.close()
        return 2
    store.close()
    print(f"initialised KB at {cfg.root}")
    print(f"  db: {cfg.db_path}")
    if policy is not None:
        print(f"  policy: {policy}")
    if args.seed:
        print("  seeded demo facts")
    # Other commands read the KB the web UI last selected, not the one we just
    # made, so point at this root explicitly rather than promise `verinote
    # status` shows it (see issue #185).
    print(f"  use this KB: VERINOTE_ROOT={cfg.root} verinote status")
    return 0


def cmd_policy_reset(cfg: Config, args: argparse.Namespace) -> int:
    """The only way a policy file is (re)created without one already existing."""
    from verinote.pipeline.policy_state import write_default_policy

    if not args.force:
        print(
            "refusing to reset the logic policy without --force: this overwrites "
            "the KB's rules with the shipped default. Restore the policy file from "
            "backup or version control if it was lost.",
            file=sys.stderr,
        )
        return 2
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = _store(cfg)
    path = write_default_policy(store, cfg.root, origin="reset")
    store.close()
    print(f"policy reset to the shipped default: {path}")
    return 0


def _seed(store: Store) -> None:
    for subj, rel, obj, status, conf, src, note in _DEMO_FACTS:
        sid = store.add_source(src)
        store.reconcile_fact(
            subj, rel, obj, status=status, confidence=conf, source_id=sid, note=note
        )


# The four ways an existing `kb.sqlite` can fail to be a usable KB. They are not
# interchangeable: `init`'s whole job is to put a schema into a database that has
# none, so only the *unreadable* one may stop it. `seed` must refuse all four —
# it fills an existing KB and never creates one.
KB_UNREADABLE = "not a readable SQLite database"
KB_NO_SCHEMA = "no `facts` table"
KB_ALIEN_FACTS = "its `facts` table is not verinote's"
KB_PARTIAL_SCHEMA = "it has a `facts` table but is missing the rest of a KB"

# What a `facts` table must have for the file to be a verinote KB at all. This is
# deliberately *not* the current schema's column set: `_ensure_schema_migrations()`
# adds columns (`job_id`) to KBs written before they existed, so requiring today's
# full set would refuse the very KBs migration exists to repair. These columns
# are the fact identity and payload — every verinote KB has always had them, and
# no migration adds them —
# so the check stays put as the schema grows around it.
_KB_FACTS_IDENTITY_COLUMNS = frozenset(
    {"id", "subject", "relation", "object", "status"}
)

# The tables that make the file a KB rather than a `facts` table someone left in a
# database. Same principle as the columns above, one level up, and chosen the same
# way: these four are every table `schema.sql` has had since the initial commit, so
# no KB verinote has ever written can lack them.
#
# This is deliberately *not* derived from today's `schema.sql`. `init_schema()` runs
# that script on every open, which makes `CREATE TABLE IF NOT EXISTS` the migration
# that adds new tables to old KBs — `kb_meta`, `fact_evidence`, `fact_events`,
# `source_artifacts`, `extraction_jobs`, `source_chunks` and `questions` all arrived
# that way. Deriving the set would therefore refuse exactly the KBs that migration
# exists to repair. The intersection over the schema's history is the part that has
# never been a migration's job, and only that part can be demanded up front.
_KB_CORE_TABLES = frozenset({"facts", "review_log", "runs", "sources"})
_KB_CORE_TABLE_COLUMNS = {
    "sources": frozenset({"id", "path"}),
    "runs": frozenset({"id"}),
    "review_log": frozenset({"id"}),
}


def _sqlite_read_only_uri(db_path: Path) -> str:
    base = db_path.resolve().as_uri()
    if db_path.with_name(db_path.name + "-wal").exists() or db_path.with_name(
        db_path.name + "-shm"
    ).exists():
        return f"{base}?mode=ro"
    return f"{base}?mode=ro&immutable=1"


def _kb_schema_problem(db_path: Path) -> str | None:
    """Say why `db_path` isn't a usable verinote KB, or None when it is one.

    A bare `is_file()` check passes for an empty or corrupt `kb.sqlite`, which
    would let `seed` silently create a schema (it must only fill an existing KB)
    or blow up with a raw `sqlite3` traceback.

    Matching the table *name* alone is not enough either. `init_schema()` is
    `CREATE TABLE IF NOT EXISTS`, so it skips a `facts` table it did not write and
    then fails on the mismatch — after having added the rest of the schema to a
    file the caller only meant to read. Checking the columns keeps that write from
    ever starting.

    The columns alone are not enough either, and for the mirror-image reason. A
    file holding nothing but a verinote-shaped `facts` table does not fail the
    mismatch: `IF NOT EXISTS` *completes* it into a real schema, and the read-only
    command then reports rc=0 on a KB it just wrote itself. Requiring the core
    tables refuses that file while it is still only a `facts` table.

    The path is percent-encoded via `Path.as_uri()` before it goes into the
    SQLite URI. Interpolating it raw truncates any root holding a `?` or `#` at
    that character, which both misreports a healthy KB as schema-less *and*
    drops `mode=ro`, so SQLite would create a stray file at the truncated path.
    """
    try:
        uri = _sqlite_read_only_uri(db_path)
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "facts" not in tables:
                return KB_NO_SCHEMA
            columns = {c["name"] for c in conn.execute("PRAGMA table_info(facts)")}
            table_columns = {
                table: {
                    c["name"] for c in conn.execute(f"PRAGMA table_info({table})")
                }
                for table in _KB_CORE_TABLE_COLUMNS
                if table in tables
            }
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return KB_UNREADABLE
    # `facts` is asked about first, and separately: "this is not verinote's `facts`"
    # is a sharper answer than "tables are missing" for a file that is somebody
    # else's, and it stays the answer it was before the core-table check existed.
    if not _KB_FACTS_IDENTITY_COLUMNS <= columns:
        return KB_ALIEN_FACTS
    missing = _KB_CORE_TABLES - tables
    if missing:
        return f"{KB_PARTIAL_SCHEMA} (no {', '.join('`' + t + '`' for t in sorted(missing))})"
    for table, required in _KB_CORE_TABLE_COLUMNS.items():
        if not required <= table_columns[table]:
            return f"{KB_PARTIAL_SCHEMA} (`{table}` is not verinote's)"
    return None


def _require_existing_kb(cfg: Config) -> int | None:
    """Refuse a read-only diagnosis when no usable KB is there, rather than
    scaffolding one. `_store()` calls `init_schema()`, so without this a mistyped
    root becomes a fresh empty KB that then reports as a healthy `0 facts` one —
    the same reason `seed` guards its own path before opening the store.
    """
    if not cfg.db_path.is_file():
        print(f"no KB at {cfg.root}", file=sys.stderr)
        return 1
    problem = _kb_schema_problem(cfg.db_path)
    if problem is not None:
        print(f"{cfg.db_path} is not a verinote KB ({problem})", file=sys.stderr)
        return 1
    return None


def _read_only_conn(cfg: Config) -> sqlite3.Connection:
    uri = _sqlite_read_only_uri(cfg.db_path)
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_only_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _status_counts_ro(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) c FROM facts GROUP BY status")
    return {row["status"]: row["c"] for row in rows}


def _sources_count_ro(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"])


def _source_fact_counts_ro(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    facts_columns = _read_only_columns(conn, "facts")
    sources_columns = _read_only_columns(conn, "sources")
    kind_expr = "s.kind" if "kind" in sources_columns else "'text'"
    if "source_id" not in facts_columns:
        return list(
            conn.execute(
                "SELECT s.id, s.path, "
                f"{kind_expr} AS kind, "
                "0 AS total, 0 AS engine "
                "FROM sources s ORDER BY s.path"
            )
        )
    placeholders = ",".join("?" for _ in engine_statuses())
    return list(
        conn.execute(
            "SELECT s.id, s.path, "
            f"{kind_expr} AS kind, "
            "COUNT(f.id) AS total, "
            f"COALESCE(SUM(CASE WHEN f.status IN ({placeholders}) "
            "THEN 1 ELSE 0 END), 0) AS engine "
            "FROM sources s LEFT JOIN facts f ON f.source_id = s.id "
            "GROUP BY s.id ORDER BY s.path",
            tuple(engine_statuses()),
        )
    )


def _policy_marker_ro(conn: sqlite3.Connection) -> dict[str, object] | None:
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "kb_meta" not in tables:
        return None
    raw = conn.execute(
        "SELECT value FROM kb_meta WHERE key = 'policy.logic'"
    ).fetchone()
    if raw is None:
        return None
    try:
        marker = json.loads(raw["value"])
    except json.JSONDecodeError:
        return {"sha256": "", "recorded_at": "", "origin": "unknown"}
    if not isinstance(marker, dict):
        return {"sha256": "", "recorded_at": "", "origin": "unknown"}
    return marker


def _policy_state_ro(conn: sqlite3.Connection, cfg: Config):
    from verinote.pipeline.policy_state import POLICY_RELPATH, PolicyState, PolicyStatus

    path = cfg.root / POLICY_RELPATH
    marker = _policy_marker_ro(conn)
    if path.is_file():
        return PolicyState(
            status=PolicyStatus.PRESENT,
            path=path,
            text=path.read_text(encoding="utf-8"),
            marker=marker,
        )
    if marker is not None:
        return PolicyState(status=PolicyStatus.MISSING_RECORDED, path=path, marker=marker)
    return PolicyState(status=PolicyStatus.UNRECORDED_DEFAULT, path=path)


def cmd_seed(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.db_path.is_file():
        print(
            f"no KB at {cfg.root}; run `verinote init` first (or name a root: "
            f"`verinote seed <path>`)",
            file=sys.stderr,
        )
        return 1
    problem = _kb_schema_problem(cfg.db_path)
    if problem is not None:
        print(
            f"{cfg.db_path} is not a verinote KB ({problem}); move it aside and run "
            f"`verinote init {cfg.root}` to scaffold one",
            file=sys.stderr,
        )
        return 1
    store = _store(cfg)
    _seed(store)
    store.close()
    print(f"seeded demo facts into {cfg.root}")
    return 0


def _rel_to_root(root: Path, p: Path) -> str:
    """Cite a source by its path relative to the KB root when it lives under it."""
    p = p.resolve()
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _resolve_sources(cfg: Config, store: Store, path: str | None) -> list[_SourceInput]:
    """Resolve a file path or registered text artifacts to extraction inputs."""
    if path:
        f = Path(path)
        if not f.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        return [_SourceInput(_rel_to_root(cfg.root, f), f.read_text(encoding="utf-8"))]

    inputs = [
        _SourceInput(
            source_path=row["source_path"],
            text=(cfg.root / row["artifact_path"]).read_text(encoding="utf-8"),
            source_id=int(row["source_id"]),
            artifact_id=int(row["artifact_id"]),
        )
        for row in store.source_text_inputs()
    ]
    if inputs:
        return inputs

    sources_dir = cfg.root / "sources"
    files = sorted(sources_dir.glob("*.txt")) + sorted(sources_dir.glob("*.md"))
    return [
        _SourceInput(_rel_to_root(cfg.root, f), f.read_text(encoding="utf-8"))
        for f in files
    ]


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.llm import LLMError, get_client
    from verinote.pipeline import (
        create_chunked_extraction_job,
        process_extraction_job,
        sync_sources,
    )
    from verinote.pipeline.policy_state import PolicyMissingError
    from verinote.prompts import PromptError

    def extraction_schema_hint() -> str:
        try:
            return cfg.extraction_schema_hint()
        except PromptError as exc:
            raise LLMError(str(exc)) from exc

    store = _store(cfg)
    try:
        sources = _resolve_sources(cfg, store, args.path)
    except (FileNotFoundError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        store.close()
        return 2
    if not sources:
        print(f"no sources to sync (looked under {cfg.root / 'sources'})", file=sys.stderr)
        store.close()
        return 1
    try:
        client = get_client(cfg)
        registered = [source for source in sources if source.source_id is not None]
        if registered:
            per_source = []
            total = 0
            run_id = 0
            for source in registered:
                job_id = create_chunked_extraction_job(
                    store,
                    source_id=source.source_id,
                    artifact_id=source.artifact_id,
                    source_text=source.text,
                    provider=cfg.provider,
                    model=cfg.model,
                    chunk_chars=cfg.extraction_chunk_chars,
                    chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
                )
                outcome = process_extraction_job(
                    store,
                    client,
                    job_id=job_id,
                    schema_hint=extraction_schema_hint(),
                )
                per_source.append((source.source_path, outcome.candidates))
                total += outcome.candidates
                run_id = job_id
            result = _SyncSummary(per_source=per_source, total=total, run_id=run_id)
        else:
            pairs = [(source.source_path, source.text) for source in sources]
            result = sync_sources(store, client, pairs, provider=cfg.provider, model=cfg.model)
    except PolicyMissingError as exc:
        # The policy vanished after the command's preflight — mid-job (the worker's
        # write boundary re-raises this after rolling the job back to `pending`) or
        # mid-batch (a later source in `sync_sources` hits the write gate). Either
        # way, report the clean halt diagnosis instead of a traceback, and exit with
        # the same rc=2 the start-of-command refusal (`_refuse_on_halted_kb`) uses.
        print(f"error: {exc}", file=sys.stderr)
        store.close()
        return 2
    except LLMError as e:
        print(f"extraction failed: {e}", file=sys.stderr)
        store.close()
        return 1
    for src, n in result.per_source:
        print(f"  {src}: {n} candidate(s)")
    print(
        f"sync complete: {result.total} candidate(s) from {len(result.per_source)} "
        f"source(s) (run #{result.run_id}) — review at `verinote ui`"
    )
    store.close()
    return 0


def cmd_ingest(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.pipeline import IngestError, ingest_file

    cfg.root.mkdir(parents=True, exist_ok=True)
    store = _store(cfg)
    try:
        result = ingest_file(store, Path(args.path), root=cfg.root)
    except IngestError as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        store.close()
        return 1
    store.close()
    print(f"ingested {args.path} -> {result['citation']} ({result['kind']})")
    print("run `verinote sync` to extract candidate facts from it")
    return 0


def cmd_query(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.llm import LLMError, get_client
    from verinote.pipeline import translate_questions, write_query_file

    store = _store(cfg)
    if args.question:
        store.add_question(args.question)
    translatable = [
        q for q in store.questions() if q["status"] in {"pending", "translation_failed"}
    ]
    if not translatable:
        print(
            "no pending or failed questions (add one: `verinote query \"...\"`)",
            file=sys.stderr,
        )
        store.close()
        return 1
    try:
        client = get_client(cfg)
    except LLMError as e:
        reason = _short_error(e)
        results = []
        for q in translatable:
            store.set_question_query(q["id"], None, "translation_failed", reason)
            results.append(
                {"id": q["id"], "status": "translation_failed", "reason": reason}
            )
        write_query_file(store, cfg.root)
    else:
        results = translate_questions(store, client, root=cfg.root)
    for r in results:
        print(f"  {format_question_outcome(r)}")
    print(f"translated {len(results)} question(s) -> {cfg.root / 'facts' / 'query.dl'}")
    print("run the check to see answers (`verinote ui` → Report)")
    store.close()
    return 0


def _short_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240]


def cmd_repair(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.llm import LLMError, get_client
    from verinote.pipeline import repair_questions

    store = _store(cfg)
    pending = [q for q in store.questions() if q["status"] == "review_required"]
    if not pending:
        print("no review_required questions to repair", file=sys.stderr)
        store.close()
        return 1
    try:
        client = get_client(cfg)
    except LLMError as e:
        print(f"repair failed: {e}", file=sys.stderr)
        store.close()
        return 1
    results = repair_questions(store, client, root=cfg.root)
    statuses = {q["id"]: q["status"] for q in store.questions()}
    repaired = sum(1 for r in results if r["accepted"])
    for r in results:
        status = r.get("status") or statuses.get(r["id"], "review_required")
        reason = "" if r["accepted"] else r.get("reason", "")
        print(
            "  "
            + format_question_outcome(
                {"id": r["id"], "text": "", "status": status, "reason": reason}
            )
        )
    print(f"repaired {repaired}/{len(results)} question(s) (engine-validated)")
    store.close()
    return 0


def _print_policy_state(store: Store) -> bool:
    """Print the KB's policy state on a diagnostic command. True when halted.

    `status` and `coverage` are the whole diagnostic surface a non-web user has,
    and before this they reported a halted KB as perfectly healthy — the only way
    to discover a lost policy was to attempt a write and be refused (#194). That is
    #155's own thesis ("a KB whose rules are gone must not look fine") surviving on
    the CLI.

    The marker goes to *stdout*, next to the rest of the summary: a banner on
    stderr alone is invisible to `verinote status > out.txt` and to a CI health
    check, which is exactly where a silent halt does its damage. The loud,
    actionable recovery text still goes to stderr, where errors belong.

    The judgement is `resolve_policy`'s alone — this function never looks at the
    filesystem. One predicate, or the enforcement points drift apart.
    """
    from verinote.pipeline.policy_state import (
        PolicyStatus,
        policy_cli_line,
        policy_missing_message,
        resolve_policy,
    )

    state = resolve_policy(store)
    print(policy_cli_line(state))
    if state.status is PolicyStatus.MISSING_RECORDED:
        print(f"error: {policy_missing_message(state)}", file=sys.stderr)
        return True
    return False


def _print_policy_state_ro(conn: sqlite3.Connection, cfg: Config) -> bool:
    from verinote.pipeline.policy_state import (
        PolicyStatus,
        policy_cli_line,
        policy_missing_message,
    )

    state = _policy_state_ro(conn, cfg)
    print(policy_cli_line(state))
    if state.status is PolicyStatus.MISSING_RECORDED:
        print(f"error: {policy_missing_message(state)}", file=sys.stderr)
        return True
    return False


def _unusable_kb(cfg: Config, exc: Exception) -> int:
    """Turn a SQLite error escaping a read-only command into a diagnosis.

    `_require_existing_kb()` can only vouch for the `facts` table; the KB is made
    of more tables than that, and checking every one of them against the current
    schema is the drift trap that check is deliberately shaped to avoid. So a
    file can still get past it and fail deeper in. Whatever it is, a command whose
    whole job is to *report* on a KB must not answer with a traceback.

    The message must not blame the KB, though, because we cannot see from here
    whether it earned the blame: a `DatabaseError` this deep is just as easily
    verinote's own SQL bug, and then the KB is healthy and "restore from backup"
    is worse counsel than the traceback this replaced — a traceback at least
    points at *us*. We cannot make that call in code, but the user can (does this
    KB open with another build?), so the restore advice waits behind that check
    instead of leading with it.
    """
    print(
        f"cannot read the KB at {cfg.root}: {exc}. This is either a KB this "
        f"version of verinote cannot read, or a bug in verinote — we cannot tell "
        f"which from here. If this KB opens with another verinote version, it is "
        f"ours: please report it. Otherwise, inspect the KB with a known-good "
        f"version before trusting this one.",
        file=sys.stderr,
    )
    return 1


def cmd_coverage(cfg: Config, args: argparse.Namespace) -> int:
    refusal = _require_existing_kb(cfg)
    if refusal is not None:
        return refusal
    try:
        return _coverage(cfg, args)
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        return _unusable_kb(cfg, exc)


def _coverage(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.engine.coverage import Coverage, SourceCoverage

    conn = _read_only_conn(cfg)
    try:
        sources = []
        for row in _source_fact_counts_ro(conn):
            path = row["path"]
            if not isinstance(path, str) or not path:
                raise ValueError("invalid source path in KB")
            engine = int(row["engine"])
            sources.append(
                SourceCoverage(
                    path=path,
                    kind=str(row["kind"]),
                    engine_facts=engine,
                    total_facts=int(row["total"]),
                    is_gap=engine == 0,
                    is_orphan=not (cfg.root / path).exists(),
                )
            )
        cov = Coverage(sources=sources)
        halted = _print_policy_state_ro(conn, cfg)
    finally:
        conn.close()
    for s in cov.sources:
        flags = []
        if s.is_gap:
            flags.append("GAP")
        if s.is_orphan:
            flags.append("ORPHAN")
        tag = ("  " + " ".join(flags)) if flags else ""
        print(f"  {s.path}: {s.engine_facts}/{s.total_facts} engine facts{tag}")
    print(
        f"coverage: {len(cov.covered)} covered, {len(cov.gaps)} gap(s), "
        f"{len(cov.orphans)} orphan(s)"
    )
    # `--strict` exists to be a machine-read gate. A KB whose rules have evaporated
    # is not a KB that passes a gate, so the halt fails it just like a coverage gap
    # does — otherwise automation greenlights a KB with no rules. Plain `coverage`
    # stays rc=0: it is a recovery path, and a halt you cannot inspect is a brick.
    if args.strict and halted:
        print("strict: this KB's logic policy file is missing", file=sys.stderr)
        return 1
    if args.strict and cov.gaps:
        print("strict: uncovered text source(s) present", file=sys.stderr)
        return 1
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    refusal = _require_existing_kb(cfg)
    if refusal is not None:
        return refusal
    try:
        return _status(cfg)
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        return _unusable_kb(cfg, exc)


def _status(cfg: Config) -> int:
    conn = _read_only_conn(cfg)
    try:
        counts = _status_counts_ro(conn)
        sources_count = _sources_count_ro(conn)
        print(f"KB: {cfg.root}")
        print(f"sources: {sources_count}")
        print(f"facts:   {sum(counts.values())}")
        for s in ("candidate", "needs_review", "confirmed", "accepted", "superseded"):
            print(f"  {s:<13} {counts.get(s, 0)}")
        # `status` stays rc=0 even when halted: it is one of the paths that must keep
        # working *on* a halted KB, and a diagnosis command that fails is not a
        # diagnosis. The stdout marker is what makes the halt impossible to miss.
        _print_policy_state_ro(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_ui(cfg: Config | None, args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("uvicorn not installed; `pip install verinote`", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}"
    if cfg is None:
        print(f"verinote ui → {url}  (select a KB in the browser)")
    else:
        print(f"verinote ui → {url}  (KB: {cfg.root})")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("verinote.web.app:_default", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def _refuse_on_halted_kb(cfg: Config | None) -> int | None:
    """Exit code to return instead of running a write on a halted KB, or None.

    Asked once, in `main`, for every subcommand that did not declare itself
    `halt_safe`. The KB's policy state itself is *never* inferred here: that
    judgement lives in `policy_state.assert_writable` alone. The db-file check is
    only "is there a KB at all" — with no database there can be no marker, hence
    nothing that could be halted, and `init` on a fresh root must still work.

    An empty or corrupt `kb.sqlite` is the same category: it holds no marker, so
    there is nothing here to halt. It must not be opened, because `init_schema()`
    would *create* a schema in it — which would hand `seed` a KB the user never
    scaffolded, and turn a corrupt file into a raw `sqlite3` traceback. Leave it
    to the subcommand, which names the problem and exits non-zero.
    """
    from verinote.pipeline.policy_state import PolicyMissingError, assert_writable

    if cfg is None or not cfg.db_path.is_file():
        return None
    if _kb_schema_problem(cfg.db_path) is not None:
        return None
    store = Store(cfg.db_path)
    store.init_schema()
    try:
        assert_writable(store)
    except PolicyMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI.

    Every subcommand declares `halt_safe`: may it run against a KB whose recorded
    logic policy file is gone? `halt_safe=True` is for the paths that must survive
    a halt — read-only diagnosis, and the recovery command itself — because a halt
    the user cannot diagnose or undo is just a bricked KB. Everything that writes
    declares False and is refused by `main`.

    `init` is deliberately *not* exempt: re-scaffolding the default policy onto a
    KB that recorded a policy would overwrite the evidence of the loss with a
    plausible-looking green KB, which is the exact failure this whole mechanism
    exists to prevent. Recovery is `policy reset --force` — an explicit human act.

    Fail closed: `main` reads this flag with a default of False, so a new
    subcommand that forgets to declare one is treated as a write and blocked.
    """
    p = argparse.ArgumentParser(prog="verinote", description="Honest KB: LLM extracts, DuckDB verifies.")
    p.add_argument("--version", action="version", version=f"verinote {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="scaffold a KB (SQLite) at ROOT (default: $VERINOTE_ROOT, else ./data in the current directory)",
    )
    init.add_argument(
        "root",
        nargs="?",
        help="where to create the KB (default: $VERINOTE_ROOT, else ./data here)",
    )
    init.add_argument("--seed", action="store_true", help="insert demo facts")
    init.set_defaults(func=cmd_init, halt_safe=False)

    seed = sub.add_parser(
        "seed",
        help="insert demo facts into an existing KB at ROOT (default: $VERINOTE_ROOT, else ./data in the current directory)",
    )
    seed.add_argument(
        "root",
        nargs="?",
        help="an existing KB root (default: $VERINOTE_ROOT, else ./data here)",
    )
    seed.set_defaults(func=cmd_seed, halt_safe=False)

    sync = sub.add_parser("sync", help="extract candidate facts from sources via the LLM")
    sync.add_argument(
        "path",
        nargs="?",
        help="a source file; omit to sync every .txt/.md under <root>/sources/",
    )
    sync.set_defaults(func=cmd_sync, halt_safe=False)

    ingest = sub.add_parser("ingest", help="register a source file (converting docx/pdf to text)")
    ingest.add_argument("path", help="a .txt/.md file, or a .docx/.pdf to convert")
    ingest.set_defaults(func=cmd_ingest, halt_safe=False)

    query = sub.add_parser("query", help="translate pending NL questions to Datalog queries")
    query.add_argument("question", nargs="?", help="a question to add before translating")
    query.set_defaults(func=cmd_query, halt_safe=False)

    repair = sub.add_parser("repair", help="re-translate review_required questions (engine-gated)")
    repair.set_defaults(func=cmd_repair, halt_safe=False)

    coverage = sub.add_parser("coverage", help="report per-source engine-fact coverage")
    coverage.add_argument(
        "--strict", action="store_true", help="exit non-zero if any text source has no engine facts"
    )
    coverage.set_defaults(func=cmd_coverage, halt_safe=True)

    status = sub.add_parser("status", help="summarise KB state")
    status.set_defaults(func=cmd_status, halt_safe=True)

    policy = sub.add_parser("policy", help="manage this KB's logic policy file")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_reset = policy_sub.add_parser(
        "reset", help="re-create the default logic policy (explicit human gate)"
    )
    policy_reset.add_argument(
        "--force",
        action="store_true",
        help="required — confirm replacing this KB's logic policy with the default",
    )
    # The one command that must run *on* a halted KB: it is how a halt is cleared.
    policy_reset.set_defaults(func=cmd_policy_reset, halt_safe=True)

    ui = sub.add_parser("ui", help="launch the web app")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8731)
    ui.add_argument("--reload", action="store_true", help="auto-reload (dev)")
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser")
    # Launchers, not writers: the web app has its own per-request halt guard, which
    # blocks writes while still serving the /report page that explains the halt.
    # Refusing to start the server would strand the user with no way to diagnose it.
    ui.set_defaults(func=cmd_ui, halt_safe=True)
    sub.add_parser("serve", help="alias for ui").set_defaults(
        func=cmd_ui, halt_safe=True, host="127.0.0.1", port=8731, reload=False, no_browser=True
    )

    return p


def _config_for(args: argparse.Namespace) -> Config | None:
    if args.command in {"ui", "serve"}:
        return Config.load_for_ui()
    if args.command in _LOCAL_ROOT_COMMANDS:
        return Config.for_root(local_root(args.root))
    return Config.load()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _config_for(args)
    except ValueError as exc:  # e.g. a blank explicit KB root
        print(str(exc), file=sys.stderr)
        return 1
    # The single CLI enforcement point for a halted KB. It sits here, before
    # dispatch, because every subcommand goes through this one line — a guard
    # sprinkled per-command is a guard the next command will forget.
    if not getattr(args, "halt_safe", False):
        refusal = _refuse_on_halted_kb(cfg)
        if refusal is not None:
            return refusal
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
