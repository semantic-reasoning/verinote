# SPDX-License-Identifier: MPL-2.0
"""verinote command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from verinote import __version__
from verinote.config import Config
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


def _store(cfg: Config) -> Store:
    store = Store(cfg.db_path)
    store.init_schema()
    return store


def _scaffold_policy(cfg: Config) -> Path | None:
    """Write the default logic policy if the KB doesn't have one yet."""
    from verinote.engine import DEFAULT_POLICY
    from verinote.pipeline.verify import POLICY_RELPATH

    path = cfg.root / POLICY_RELPATH
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_POLICY, encoding="utf-8")
    return path


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = _store(cfg)
    if args.seed:
        _seed(store)
    store.close()
    policy = _scaffold_policy(cfg)
    print(f"initialised KB at {cfg.root}")
    print(f"  db: {cfg.db_path}")
    if policy is not None:
        print(f"  policy: {policy}")
    if args.seed:
        print("  seeded demo facts (run `verinote status`)")
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


def _resolve_sources(cfg: Config, path: str | None) -> list[tuple[str, str]]:
    """Resolve a file path (or all sources under `<root>/sources/`) to (citation, text)."""
    if path:
        f = Path(path)
        if not f.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        files = [f]
    else:
        sources_dir = cfg.root / "sources"
        files = sorted(sources_dir.glob("*.txt")) + sorted(sources_dir.glob("*.md"))
    return [(_rel_to_root(cfg.root, f), f.read_text(encoding="utf-8")) for f in files]


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    from verinote.llm import LLMError, get_client
    from verinote.pipeline import sync_sources

    store = _store(cfg)
    try:
        sources = _resolve_sources(cfg, args.path)
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
        result = sync_sources(store, client, sources, provider=cfg.provider, model=cfg.model)
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
    from verinote.pipeline import translate_questions

    store = _store(cfg)
    if args.question:
        store.add_question(args.question)
    pending = store.questions(pending_only=True)
    if not pending:
        print("no pending questions (add one: `verinote query \"...\"`)", file=sys.stderr)
        store.close()
        return 1
    try:
        client = get_client(cfg)
        results = translate_questions(store, client, root=cfg.root)
    except LLMError as e:
        print(f"translation failed: {e}", file=sys.stderr)
        store.close()
        return 1
    for r in results:
        print(f"  q{r['id']}: {r['status']}")
    print(f"translated {len(results)} question(s) -> {cfg.root / 'facts' / 'query.dl'}")
    print("run the check to see answers (`verinote ui` → Report)")
    store.close()
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


def cmd_ui(cfg: Config, args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("uvicorn not installed; `pip install verinote`", file=sys.stderr)
        return 1
    # Ensure the KB exists before serving.
    _store(cfg).close()
    url = f"http://{args.host}:{args.port}"
    print(f"verinote ui → {url}  (KB: {cfg.root})")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("verinote.web.app:_default", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="verinote", description="Honest KB: LLM extracts, wirelog verifies.")
    p.add_argument("--version", action="version", version=f"verinote {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="scaffold a local KB (SQLite) under VERINOTE_ROOT (./data)")
    init.add_argument("--seed", action="store_true", help="insert demo facts")
    init.set_defaults(func=cmd_init)

    seed = sub.add_parser("seed", help="insert demo facts into the KB")
    seed.set_defaults(func=cmd_seed)

    sync = sub.add_parser("sync", help="extract candidate facts from sources via the LLM")
    sync.add_argument(
        "path",
        nargs="?",
        help="a source file; omit to sync every .txt/.md under <root>/sources/",
    )
    sync.set_defaults(func=cmd_sync)

    ingest = sub.add_parser("ingest", help="register a source file (converting docx/pdf to text)")
    ingest.add_argument("path", help="a .txt/.md file, or a .docx/.pdf to convert")
    ingest.set_defaults(func=cmd_ingest)

    query = sub.add_parser("query", help="translate pending NL questions to Datalog queries")
    query.add_argument("question", nargs="?", help="a question to add before translating")
    query.set_defaults(func=cmd_query)

    status = sub.add_parser("status", help="summarise KB state")
    status.set_defaults(func=cmd_status)

    ui = sub.add_parser("ui", help="launch the web app")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8731)
    ui.add_argument("--reload", action="store_true", help="auto-reload (dev)")
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ui.set_defaults(func=cmd_ui)
    sub.add_parser("serve", help="alias for ui").set_defaults(func=cmd_ui, host="127.0.0.1", port=8731, reload=False, no_browser=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
