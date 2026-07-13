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

You can also scaffold a KB explicitly. `init` and `seed` are *local* commands:
they never write to the saved active KB. They target the root you name, else
`VERINOTE_ROOT`, else `./data` in the current directory.

```bash
verinote init                 # ./data here (or $VERINOTE_ROOT if set)
verinote init /path/to/kb     # a named root
verinote seed /path/to/kb     # demo facts into an existing KB
```

Creating a KB does not make it the active one. Every other command still reads
the saved active KB, so to work with the KB you just created either point
`VERINOTE_ROOT` at it or select it in the UI:

```bash
VERINOTE_ROOT=/path/to/kb verinote status
```

Seeded demo facts land as `candidate`/`needs_review`, never as engine input —
demo data has to pass through human review like anything else.

DuckDB is a core dependency because it powers verification. The `analytics` extra is
kept as a compatibility no-op; analytics uses the same DuckDB dependency. The
`wirelog` extra installs the legacy `pyrewire` path for compatibility/debugging only.

## Your KB is not a repo artifact

A KB holds **user data**, so verinote never commits it. Treat the whole KB root
as source: **the only file in it that can be rebuilt is `facts/query.dl`.**
Everything else, if you lose it, is gone.

| Path (KB root) | What it holds | Irreplaceable because |
| --- | --- | --- |
| `kb.sqlite` | facts, sources, questions | it records **every accept/reject decision and the full audit log** |
| `facts.duckdb` | the canonical logical fact terms | see below — it is **not** rebuildable from `kb.sqlite` |
| `sources/` | the documents you ingested, byte for byte | verinote never re-downloads them |
| `artifacts/` | the extracted text of each source | `kb.sqlite` stores only its path and checksum, not the text |
| `policy/logic-policy.dl` | your review rules | scaffolded by `init`, then hand-edited |
| `policy/relation-aliases.md` | raw -> canonical relation names | hand-written |
| `policy/typed-relations.md` | typed relation declarations | hand-written |
| `policy/prompts/` | your prompt overrides | hand-written |
| `config.json` | provider/model settings | hand-written |

`init` writes a starting `logic-policy.dl` for you, but every edit you make to it
afterwards is yours alone and is not reproducible from this repo — which is why
it must stay committable rather than be swept up by a blanket `*.dl` ignore rule.

**`facts.duckdb` is data, not a cache.** It owns the logical fact terms; the
`facts.subject/relation/object` columns in `kb.sqlite` are display mirrors, and
rendering a term into text is lossy. If the sidecar goes missing, the terms are
*re-typed* from those mirrors rather than restored: a compound collapses into a
string, and even a plain string gains the quotes it was rendered with.

```text
before:  Compound('person', (StringLit('Ada'),))   Atom('works_at')       StringLit('Acme')
after:   StringLit('person("Ada")')                StringLit('works_at')  StringLit('"Acme"')
```

Rules that matched the structural terms stop firing, and the report still says
the knowledge base is consistent. Silent re-typing on a lost sidecar is tracked
in [#156](https://github.com/semantic-reasoning/verinote/issues/156) — until it
is fixed, **back the sidecar up with the rest of the KB.**

**Keep the KB outside this working tree.** The default root (`./data`) is a
convenience for a first run, not a safe home:

```bash
VERINOTE_ROOT=~/verinote-kb verinote init   # scaffold a KB outside the repo
VERINOTE_ROOT=~/verinote-kb verinote ui
```

`VERINOTE_ROOT` selects the KB root for every command, so exporting it once in
your shell profile keeps all of your data out of this working tree.

**If you do keep a KB inside the repo tree** (any path other than `data/`, e.g.
`./my-kb`), a stray `git add -A` will commit most of it. Only the generated
artifacts are ignored; the sources are not, and that is deliberate — the ignore
rules match generated *paths*, never bare extensions, because a blanket `*.dl`
rule is exactly what used to swallow hand-written policy. What gets staged:

```text
my-kb/sources/confidential.pdf        <- the document you ingested, byte for byte
my-kb/artifacts/sources/1/<sha>.txt   <- its extracted text
my-kb/policy/logic-policy.dl          <- your review rules
my-kb/config.json                     <- your provider/model settings
```

So the exposure is not one policy file — it is **the documents themselves**, which
is exactly what [AGENTS.md](AGENTS.md) forbids committing. Keep the KB outside
the tree, or do not blind-add.

**`git clean -fdx` deletes your KB.** Ignoring it does not save it: `-x` removes
ignored files too, and user data cannot be committed to fix that. (Without `-x`,
ignoring *does* protect the generated artifacts — but not `sources/` or
`policy/`, which are not ignored.) Running it inside the working tree destroys
the KB and its audit log with no undo. Moving the default root outside the
working tree is tracked in
[#185](https://github.com/semantic-reasoning/verinote/issues/185).

**Backups are your responsibility.** verinote takes none. Copy **the whole KB
root** — it is a plain folder — on your own schedule:

```bash
cp -a ~/verinote-kb ~/backups/verinote-kb-$(date +%F)
```

Do not snapshot only part of it. `kb.sqlite` alone is not a backup: without
`facts.duckdb` the fact terms come back re-typed (see above), and without
`sources/` and `artifacts/` the provenance behind every confirmed fact is gone.

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

## Ask Output Order

The Ask tab is evidence-first. Once a question is routed, verinote shows the
answer block immediately under its route label (`VERIFIED — engine`,
`VERIFIED — engine (negative)`, or `UNVERIFIED — source exploration`) before
route reasons, query details, source tables, or excerpts.

Treat that first block as the evidence. Any surrounding explanation must follow
it and stay short; do not restate, translate, or summarize the block rows before
the user has seen them.

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.
