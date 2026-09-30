# Architecture

## Fact storage boundary

Each KB stores two coordinated files under the KB root:

- **`kb.sqlite`** — sources, extraction runs, review status, audit history,
  questions, and the `facts.subject/relation/object` text columns used as display
  mirrors and legacy backfill data.
- **`facts.duckdb`** — canonical logical fact terms, keyed by SQLite `facts.id`.

Verification and report fact input read confirmed/accepted fact ids from SQLite,
then load their logical terms from `facts.duckdb`. Source coverage, status counts,
and analytics use SQLite metadata; relation analytics intentionally summarize the
SQLite display mirror rather than acting as logical inference input.

For those metadata aggregates, DuckDB **attaches the SQLite file read-only**
(`ATTACH … (TYPE sqlite, READ_ONLY)`) rather than copying rows across the
boundary — the same DuckDB dependency that backs the engine also serves analytics,
and it can never write to the KB while doing so.

Because the mirrors are lossy, the sidecar is data and not a cache — losing it is
an unrecoverable failure, not a rebuild. See
[operations.md](operations.md#factsduckdb-is-data-not-a-cache).

## Form responses as sources

A Form Sync check turns one batch of new response rows into **one immutable
source** — `sources/form-<SHEET-ID>-<stamp>.txt` — and hands it to the existing
chunked extraction pipeline. There is no separate "form" extraction path: the
batch is a source exactly like an ingested document, so review, corroboration,
verification, and provenance all treat it the same.

The one-source-per-batch split is the audit trail: a fact can be traced to
the *exact batch* it came from, and the batch survives in `sources/` byte for
byte even if its extraction later fails (re-analyse with `verinote sync
<path>`). An empty batch materialises nothing — no source row, no job — and a
citation collision is refused before any side effect, because
`Store.add_source` upserts by path and a second batch on the same path would
silently overwrite a source row whose candidates a human may already have
judged.

Two invariants keep the Google side honest:

- **Single caller per grant.** The credential capture, the token refresh, the sheet read, and
  the rotated-token persist are one critical section under one lock (the
  worker's `sheet_lock`; the CLI holds one lock for the whole command).
  Google rotates refresh tokens, so two concurrent readers of the same grant
  would each capture the pre-rotation token, and the loser would die with an
  `invalid_grant` that looks exactly like expiry. Serialising them, and
  persisting the rotated token *inside* the section, is what makes "the
  stored token is the live one" true across checks.
- **Non-monotonic watermark.** The watermark is a row *position*, not a high
  water mark: it may move down when rows are deleted, and that is intended —
  the returning rows must be re-read, and a monotonic mark would skip them
  forever.

## Term typing

Plain extractor output remains `StringLit` by default, so text such as
`person("Ada")` is **not** reinterpreted as a compound term. Source extraction can
produce structural facts only by explicitly marking a slot as a term:

```json
{"kind": "term", "value": "person(\"Ada\")"}
```

Structural facts can also be entered through explicit term mode or
`structural_term(...)`. Legacy SQLite rows without DuckDB term rows are backfilled
as `StringLit` values the first time they are selected for verification.

## Relation canonicalization

New extraction prefers stable English canonical relation labels such as `role`,
`affiliation`, and `provides`. Source-language labels remain supported through
`policy/relation-aliases.md`, where each line maps a source or local label to the
canonical relation:

```text
- `역할` -> `role`
- `제공 요소` -> `provides`
```

Subjects and objects preserve the source document's language and named-entity
spelling. Relation aliases are used by extraction, query planning, trust views, and
verification query expansion, so older source-language facts can still answer
canonical English questions.

## Ask output order

The Ask tab is evidence-first. Once a question is routed, verinote shows the answer
block immediately under its route label — `VERIFIED — engine`,
`VERIFIED — engine (negative)`, or `UNVERIFIED — source exploration` — before route
reasons, query details, source tables, or excerpts.

Treat that first block as the evidence. Any surrounding explanation must follow it
and stay short; do not restate, translate, or summarize the block rows before the
user has seen them.

## Synthetic Ask capture

`docs/img/ask-verified.png` is a reproducible local UI capture, not a product
mockup. Use a throwaway KB outside the repository and only these synthetic values:

```bash
ROOT=/tmp/verinote-ask-verified-demo
uv run verinote init "$ROOT"
uv run python - <<'PY'
from pathlib import Path
from verinote.pipeline.ingest import store_source
from verinote.store import Store

root = Path("/tmp/verinote-ask-verified-demo")
store = Store(root / "kb.sqlite")
store.init_schema()
text = "Example Org states that its purpose is a synthetic verification demo.\n"
source = store_source(store, root, "example-org-brief.txt", text.encode("utf-8"), text, "text")
fact = store.add_fact("Example Org", "purpose", "synthetic verification demo",
                      status="confirmed", confidence=1.0, source_id=source["source_id"])
store.add_fact_evidence(fact_id=fact, source_id=source["source_id"],
                        artifact_id=source["artifact_id"], evidence_kind="statement",
                        locator="line 1", snippet=text.strip())
store.close()
PY
VERINOTE_ROOT="$ROOT" VERINOTE_PROVIDER=ollama VERINOTE_MODEL=synthetic \
  uv run verinote ui --no-browser --port 8732
```

With Chrome Headless at a 1440x1100 viewport, open `/ask`, submit `What is the
purpose of Example Org?`, and capture the resulting page to
`docs/img/ask-verified.png`. Verify in the rendered DOM and image that the
question, `VERIFIED — engine`, answer block, and the `Source` and `Evidence`
columns appear in that order. The deterministic question shape avoids any model
request; the Ollama setting only supplies the local adapter instance.
