# verinote

[![ci](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml/badge.svg)](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Ask your documents a question. Get an answer a deterministic engine has verified — with the source to prove it.**

<!-- TODO: demo.gif — Ask tab showing "VERIFIED — engine" (#212) -->

For anyone whose notes, docs, and meeting minutes pile up until facts drift, go
stale, and lose the source that justified them — and who has to *trust* an answer,
not just read one. It runs as a local web app for a single user, on your own
machine.

verinote is an honest knowledge base that runs as a local web app. An LLM extracts
source-backed candidate facts from your documents; a DuckDB-backed Datalog engine
verifies them deterministically; and by default nothing becomes engine input until
a human approves it.

## Why

Two hundred meeting notes, and nobody knows which decision is still current. Every
free-text wiki drifts: facts go stale, contradict each other, and lose the link to
whatever document justified them — and once you bolt an LLM on top, it happily
summarizes the drift with full confidence.

Three things set verinote apart:

1. **A human holds the gate.** Nothing becomes engine input until a person
   promotes it from the review tier (`candidate`/`needs_review`) to an engine
   status (`confirmed`/`accepted`) — and not even seeded demo facts skip the
   queue. One opt-in rule can promote corroborated, conflict-free facts for you,
   but turning it on is you delegating the gate, not removing it
   ([auto-accept](docs/configuration.md#auto-accept)).
2. **`VERIFIED` is a deterministic proof, not a generation.** The label means a
   deterministic Datalog query derived the answer from facts you approved; the
   model never gets to decide what is true. When the engine cannot answer,
   verinote says so with `UNVERIFIED — source exploration` rather than faking it.
3. **Local-first and vendor-neutral by design.** verinote is a local single-user
   web app with swappable LLM adapters (Anthropic, Claude CLI, OpenAI, or local
   Ollama). There is no cloud service and no lock-in — that is a design
   principle, not a missing feature.

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

## How is this different from RAG or a wiki?

Both retrieval-augmented generation and a wiki hand you text; verinote hands you a
judgment about that text — and tells you who made it.

| Axis | RAG | Wiki (± LLM plugins) | verinote |
|---|---|---|---|
| The answer is | generated — the model reads retrieved text and writes a reply | whatever a human last wrote (a plugin summarizes on top) | a deterministic engine result (`VERIFIED`) or explicitly unverified excerpts (`UNVERIFIED`) |
| What decides truth | the model | the last editor | a deterministic Datalog engine over facts you approved |
| Provenance | retrieval cites chunks, but the answer text isn't bound to them | manual links that drift as pages are edited | every fact carries recorded provenance; a verified answer echoes the fact and its sources |
| Stale facts | no lifecycle — an old chunk retrieves the same | silently overwritten or left contradictory | retired via `superseded`; single-valued conflicts are flagged, not overwritten |
| Runs | usually a hosted vector DB / API | a hosted service | a local single-user web app with swappable LLM adapters |

Source-language facts still answer canonical questions through relation aliases;
that mapping and the rest of the query path are in
[docs/architecture.md](docs/architecture.md).

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

## Contributing

- **Try it.** Point verinote at a real document set and walk it through the
  [Quickstart](#quickstart).
- **Tell us where it surprised you.** Open an issue on the
  [issue tracker](https://github.com/semantic-reasoning/verinote/issues) —
  especially where extraction or verification did something you did not expect.

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.

## Acknowledgements

verinote borrows its core concept from
[factlog](https://github.com/semantic-reasoning/factlog) (neurosymbolic: an LLM
extracts, a Datalog engine verifies) but is a from-scratch implementation with no
shared code.
