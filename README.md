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

## Quickstart

```bash
pip install -e ".[anthropic,test]"   # pick the providers you use
verinote ui        # launch the web app at http://localhost:8731
```

On first launch, if verinote cannot find an active KB, the web app opens a KB
selection screen. Choose a KB folder there; if the folder has no `kb.sqlite`,
verinote creates one. On later launches, the app opens that KB directly.

The active KB path is saved in a platform-native app config file:

- Windows: `%APPDATA%\verinote\app.json`
- macOS: `~/Library/Application Support/verinote/app.json`
- Linux/Unix: `${XDG_CONFIG_HOME:-~/.config}/verinote/app.json`

`VERINOTE_ROOT` overrides the saved active KB and is still useful for scripts,
tests, and one-off launches.

```bash
VERINOTE_ROOT=/path/to/kb verinote ui
```

You can also scaffold a KB explicitly:

```bash
verinote init      # uses VERINOTE_ROOT, the saved active KB, or ./data
```

DuckDB is a core dependency because it powers verification. The `analytics` extra is
kept as a compatibility no-op; analytics uses the same DuckDB dependency. The
`wirelog` extra installs the legacy `pyrewire` path for compatibility/debugging only.

## Your KB is not a repo artifact

A KB holds **user data**, so verinote never commits it. Nothing in it can be
regenerated from this repo — if you lose it, it is gone:

| Path (KB root) | What it holds | Irreplaceable because |
| --- | --- | --- |
| `kb.sqlite` | facts, sources, questions | it records **every accept/reject decision and the full audit log** |
| `policy/logic-policy.dl` | your review rules | hand-written |
| `policy/relation-aliases.md` | raw -> canonical relation names | hand-written |
| `policy/typed-relations.md` | typed relation declarations | hand-written |

Only `facts/query.dl` and `facts.duckdb` are rebuilt from `kb.sqlite`.

**Keep the KB outside this working tree.** The default root (`./data`) is a
convenience for a first run, not a safe home:

```bash
verinote init ~/verinote-kb          # scaffold a KB outside the repo
VERINOTE_ROOT=~/verinote-kb verinote ui
```

**`git clean -fdx` deletes your KB**, and `-x` makes ignoring it irrelevant:
`clean` removes *untracked* files whether or not they are ignored, and user data
cannot be committed to fix that. Running it inside the working tree destroys the
KB and its audit log with no undo. Moving the default root outside the working
tree is tracked in
[#185](https://github.com/semantic-reasoning/verinote/issues/185).

**Backups are your responsibility.** verinote takes none. Copy the KB root (it
is a plain folder) or snapshot `kb.sqlite` plus `policy/` on your own schedule.

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
`person("Ada")` is not reinterpreted as a compound term. Source extraction can
produce structural facts only by explicitly marking a slot as a term, for example
`{"kind": "term", "value": "person(\"Ada\")"}`. Structural facts can also be
entered through explicit term mode or `structural_term(...)`. Legacy SQLite rows
without DuckDB term rows are backfilled as `StringLit` values the first time they
are selected for verification.

## Relation Canonicalization

New extraction prefers stable English canonical relation labels such as `role`,
`affiliation`, and `provides`. Source-language labels remain supported through
`policy/relation-aliases.md`, where each line maps a source or local label to the
canonical relation:

```text
- `역할` -> `role`
- `제공 요소` -> `provides`
```

Subjects and objects preserve the source document's language and named-entity
spelling. Relation aliases are used by extraction, query planning, trust views,
and verification query expansion so older source-language facts can still answer
canonical English questions.

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.
