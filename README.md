# verinote

[![ci](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml/badge.svg)](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml)

**Honest knowledge base.** An LLM extracts source-backed *candidate* facts from your
documents; a deterministic DuckDB-backed Datalog engine verifies them; you keep a
human review gate before any fact is promoted to engine input. Runs as a local web app.

> Borrows the *concept* of [factlog](https://github.com/semantic-reasoning/factlog)
> (neurosymbolic: LLM extracts, a Datalog engine verifies) but is a
> from-scratch implementation — no shared code.

## Why

A free-text wiki drifts: facts go stale, contradict each other, and lose their
sources. verinote keeps the knowledge base *honest* by pairing a neural extractor
with a symbolic verifier:

- **LLM extracts** source-backed candidate facts (provider-agnostic — see below).
- **DuckDB-backed Datalog verifies** consistency deterministically. Because every fact is
  re-checked by the engine, swapping to a cheaper or local model never compromises
  correctness.
- **You review.** Facts move `candidate → needs_review → confirmed/accepted`
  through a human gate; `superseded` retires them.

## Design (locked)

| Concern        | Decision |
|----------------|----------|
| Logic engine   | **DuckDB-backed Datalog**. Confirmed rows load into in-memory DuckDB and non-recursive policy/query rules compile to SQL. |
| LLM            | **Hand-rolled `LLMClient` adapters** (Anthropic / OpenAI / Ollama). No vendor lock-in. |
| Web            | **FastAPI + HTMX + Jinja** — server-rendered partials, no JS build step. |
| Storage        | **SQLite** owns KB metadata, source/run provenance, review lifecycle, audit log, and text display mirrors. **DuckDB** owns logical fact terms in `facts.duckdb`, runs verification, and attaches SQLite read-only for metadata analytics. `.dl` policy/query files remain editable inputs. |

## Status

Early scaffold (`v0.0.1`). Working today: SQLite store, LLM adapters, review queue,
DuckDB-backed verification, question translation/repair, and local web UI.

### v1 vertical slice

1. Upload a text source → DB
2. Auto-extract candidates via one LLM adapter
3. **Review queue UI** (toggle / accept / reject)
4. Load confirmed facts into DuckDB → run Datalog policy/query rules → show report
5. Dashboard

Deferred: NL→Datalog query, gated self-correction (repair), coverage critic,
multi-provider expansion.

## Quickstart

```bash
pip install -e ".[anthropic,test]"   # pick the providers you use
verinote init      # scaffold a local KB (SQLite) under ./data
verinote ui        # launch the web app at http://localhost:8731
```

DuckDB is a core dependency because it powers verification. The `analytics` extra is
kept as a compatibility no-op; analytics uses the same DuckDB dependency. The
`wirelog` extra installs the legacy `pyrewire` path for compatibility/debugging only.

## Fact Storage Boundary

Each KB stores two coordinated files under the KB root:

- `kb.sqlite`: sources, extraction runs, review status, audit history, questions,
  and the `facts.subject/relation/object` text columns used as display mirrors
  and legacy backfill data.
- `facts.duckdb`: canonical logical fact terms keyed by SQLite `facts.id`.

Verification and report fact input read confirmed/accepted fact ids from SQLite,
then load their logical terms from `facts.duckdb`. Source coverage, status counts,
and analytics use SQLite metadata; relation analytics intentionally summarize the
SQLite display mirror rather than acting as logical inference input.

Plain extractor output remains `StringLit` by default, so text such as
`person("Ada")` is not reinterpreted as a compound term. Structural facts must be
entered through explicit term mode or `structural_term(...)`. Legacy SQLite rows
without DuckDB term rows are backfilled as `StringLit` values the first time they
are selected for verification.

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.
