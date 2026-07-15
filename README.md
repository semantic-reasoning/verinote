# verinote

[![ci](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml/badge.svg)](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Ask your documents a question. Get an answer a deterministic engine has verified — with the source to prove it.**

verinote is an honest knowledge base that runs as a local web app. An LLM extracts
source-backed candidate facts from your documents; a DuckDB-backed Datalog engine
verifies them deterministically; and by default nothing becomes engine input until
a human approves it.

## Why

Two hundred meeting notes, and nobody knows which decision is still current. Every
free-text wiki drifts: facts go stale, contradict each other, and lose the link to
whatever document justified them — and once you bolt an LLM on top, it happily
summarizes the drift with full confidence.

verinote keeps the knowledge base honest by splitting the work three ways:

1. **The LLM only proposes.** It extracts candidate facts, each tied to a source
   document (provider-agnostic: Anthropic, Claude CLI, OpenAI, or local Ollama).
2. **The engine only verifies.** Confirmed facts load into DuckDB, where Datalog
   policy and query rules check them deterministically. Because every fact is
   re-checked by the engine, swapping to a cheaper or local model never
   compromises correctness.
3. **You decide.** A fact sits in the review tier (`candidate` or `needs_review`)
   until you promote it to an engine status (`confirmed` or `accepted`);
   `superseded` retires it. Not even seeded demo facts skip the queue. One
   opt-in rule can promote corroborated, conflict-free facts for you — it is off
   by default, and turning it on is you delegating the gate, not removing it
   ([auto-accept](docs/configuration.md#auto-accept)).

When you ask a question, the answer arrives labeled with how much you can trust
it: **`VERIFIED — engine`** when the Datalog engine proved it from facts you
approved, or **`UNVERIFIED — source exploration`** when verinote is only
surfacing excerpts. The evidence block always comes first; commentary follows it.

## Quickstart

```bash
pip install -e ".[anthropic,test]"   # pick the providers you use
verinote ui                          # opens http://127.0.0.1:8731
```

On first launch verinote asks you to pick a KB folder (creating `kb.sqlite` if
needed) and remembers it for next time.

> **Keep your KB outside this working tree.** A KB is user data, not a repo
> artifact. `VERINOTE_ROOT=~/verinote-kb verinote ui` is the safe habit — see
> [Your data lives in one folder](#your-data-lives-in-one-folder).

CLI scaffolding (`verinote init`, `verinote seed`), the active-KB config file
locations, and `VERINOTE_ROOT` precedence rules are covered in
[docs/configuration.md](docs/configuration.md).

## How is this different from RAG?

RAG retrieves text and hopes the model reads it correctly — the answer is still a
generation. In verinote, an answer marked `VERIFIED` is not a generation: it is
the output of a deterministic Datalog query over facts that passed the review
gate, each with recorded provenance. The LLM's judgment is confined to proposing
candidates; it never gets to decide what is true.

Compared to a wiki (with or without LLM plugins), the difference is the review
lifecycle and the audit log: every accept/reject decision is recorded, stale
facts are retired via `superseded` rather than silently overwritten, and
source-language facts still answer canonical English questions through relation
aliases.

## Design (locked)

| Concern | Decision |
|---|---|
| Logic engine | **DuckDB-backed Datalog.** Confirmed rows load into in-memory DuckDB; non-recursive policy/query rules compile to SQL. |
| LLM | **Hand-rolled `LLMClient` adapters** (Anthropic / Claude CLI / OpenAI / Ollama). No vendor lock-in. |
| Web | **FastAPI + HTMX + Jinja** — server-rendered partials, no JS build step. |
| Storage | **SQLite** owns KB metadata, source/run provenance, review lifecycle, the audit log, and the text display mirrors. **DuckDB** owns the canonical logical fact terms, runs verification, and attaches SQLite read-only for metadata analytics. `.dl` policy/query files stay editable inputs. |

Internals — the SQLite/DuckDB fact-storage boundary, term typing (`StringLit` vs
structural terms), relation canonicalization, and the Ask tab's evidence-first
output contract — are documented in
[docs/architecture.md](docs/architecture.md).

## Status

Early scaffold (`v0.0.1`). Working today: SQLite store, LLM adapters, review
queue, DuckDB-backed verification, question translation/repair, and the local
web UI.

**v1 vertical slice**

1. Upload a text source → DB
2. Auto-extract candidates via one LLM adapter
3. **Review queue UI** (toggle / accept / reject)
4. Load confirmed facts into DuckDB → run Datalog policy/query rules → show report
5. Dashboard

## Your data lives in one folder

Three rules keep it safe:

1. **Keep the KB outside the repo.** `VERINOTE_ROOT=~/verinote-kb` in your shell
   profile does it once for every command. A KB inside the working tree can be
   committed by a stray `git add -A` or destroyed by `git clean -fdx`.
2. **Back up the whole KB root, on your own schedule.** It is a plain folder:
   `cp -a ~/verinote-kb ~/backups/verinote-kb-$(date +%F)`. verinote takes no
   backups for you.
3. **Never snapshot part of it.** `kb.sqlite` alone is not a backup. Without
   `facts.duckdb` the engine refuses to run rather than guess at the fact terms,
   and without `sources/` and `artifacts/` the provenance behind every confirmed
   fact is gone.

What each file holds, why the DuckDB sidecar is data rather than a cache, and the
exact failure modes are in [docs/operations.md](docs/operations.md).

## Contributing & roadmap

Issues and feedback are welcome — the v1 vertical slice above is the current
roadmap, and open questions are tracked in the
[issue tracker](https://github.com/semantic-reasoning/verinote/issues). If you
try verinote on a real document set, we would especially like to hear where
extraction or verification surprised you.

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.

## Acknowledgements

verinote borrows its core concept from
[factlog](https://github.com/semantic-reasoning/factlog) (neurosymbolic: an LLM
extracts, a Datalog engine verifies) but is a from-scratch implementation with no
shared code.
