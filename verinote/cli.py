# SPDX-License-Identifier: Apache-2.0
"""verinote command-line entrypoint."""

from __future__ import annotations

import argparse
import sys

from verinote import __version__
from verinote.config import Config
from verinote.store import Store

_DEMO_FACTS = [
    # (subject, relation, object, status, confidence, source, note)
    ("AI프렌즈학회", "is_a", "참여기관", "needs_review", 0.95, "sources/grant-0403.txt", "참여기관"),
    ("AI프렌즈학회", "established_on", "2021.08.04", "confirmed", 0.98, "sources/grant-0403.txt", ""),
    ("Wirelog 과제", "has_participant", "AI프렌즈학회", "confirmed", 0.92, "sources/grant-0403.txt", ""),
    ("wirelog", "is_a", "deterministic logic engine", "candidate", 0.90, "sources/workshop.txt", ""),
]


def _store(cfg: Config) -> Store:
    store = Store(cfg.db_path)
    store.init_schema()
    return store


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    cfg.root.mkdir(parents=True, exist_ok=True)
    store = _store(cfg)
    if args.seed:
        _seed(store)
    store.close()
    print(f"initialised KB at {cfg.root}")
    print(f"  db: {cfg.db_path}")
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
