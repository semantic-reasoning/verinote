# SPDX-License-Identifier: MPL-2.0
"""verinote command-line entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from pathlib import Path

from verinote import __version__
from verinote.config import Config
from verinote.pipeline.question_outcome import format_question_outcome
from verinote.store import Store

_DEMO_FACTS = [
    # (subject, relation, object, status, confidence, source, note)
    # Obviously-fictional placeholder data: it only demonstrates the status
    # lifecycle and the (subject, relation, object) shape. Do NOT put real
    # organisations, people, or grant references here.
    ("Example Org", "is_a", "participant", "needs_review", 0.95, "sources/example-grant.txt", "participant"),
    ("Example Org", "established_on", "2020-01-01", "confirmed", 0.98, "sources/example-grant.txt", ""),
    ("Demo Project", "has_participant", "Example Org", "confirmed", 0.92, "sources/example-grant.txt", ""),
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
        print("  seeded demo facts (run `verinote status`)")
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
        store.add_fact(subj, rel, obj, status=status, confidence=conf, source_id=sid, note=note)


def cmd_seed(cfg: Config, args: argparse.Namespace) -> int:
    store = _store(cfg)
    _seed(store)
    store.close()
    print("seeded demo facts")
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


def cmd_coverage(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.engine import coverage

    store = _store(cfg)
    cov = coverage(store, root=cfg.root)
    store.close()
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
    if args.strict and cov.gaps:
        print("strict: uncovered text source(s) present", file=sys.stderr)
        return 1
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    store = _store(cfg)
    counts = store.status_counts()
    print(f"KB: {cfg.root}")
    print(f"sources: {len(store.sources())}")
    print(f"facts:   {sum(counts.values())}")
    for s in ("candidate", "needs_review", "confirmed", "accepted", "superseded"):
        print(f"  {s:<13} {counts.get(s, 0)}")
    store.close()
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
    """
    from verinote.pipeline.policy_state import PolicyMissingError, assert_writable

    if cfg is None or not cfg.db_path.is_file():
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

    init = sub.add_parser("init", help="scaffold a local KB (SQLite) under VERINOTE_ROOT (./data)")
    init.add_argument("--seed", action="store_true", help="insert demo facts")
    init.set_defaults(func=cmd_init, halt_safe=False)

    seed = sub.add_parser("seed", help="insert demo facts into the KB")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load_for_ui() if args.command in {"ui", "serve"} else Config.load()
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
