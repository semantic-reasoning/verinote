# Operating a verinote KB

Your KB is user data. verinote never commits it, never backs it up, and — for the
one file that matters most — cannot rebuild it. This page is what you need to know
before you trust a KB with real documents.

## Your KB is not a repo artifact

Treat the whole KB root as source. Almost nothing in it can be regenerated, and
the two things that can are not the things you care about: `facts/query.dl` is
recompiled from your questions, and `policy/logic-policy.dl` can be re-scaffolded
— but only back to the **shipped default**, not to whatever you edited it into.

| Path (KB root) | What it holds | Irreplaceable because |
|---|---|---|
| `kb.sqlite` | facts, sources, questions | it records **every accept/reject decision and the full audit log** |
| `facts.duckdb` | the canonical logical fact terms | see below — it is **not** rebuildable from `kb.sqlite` |
| `sources/` | the documents you ingested, byte for byte | verinote never re-downloads them |
| `artifacts/` | the extracted text of each source | `kb.sqlite` stores only its path and checksum, not the text. Re-derivable by re-ingesting — but only while `sources/` survives |
| `policy/logic-policy.dl` | your review rules | scaffolded by `init`; `policy reset` only restores the default, never your edits |
| `policy/relation-aliases.md` | raw → canonical relation names | written by hand or by the Settings UI |
| `policy/typed-relations.md` | typed relation declarations | hand-written; nothing in verinote writes it |
| `policy/prompts/` | your prompt overrides | written by hand or by the Prompts UI |
| `config.json` | provider/model settings | written by hand or by the Settings UI |

Every edit you make to `logic-policy.dl` is yours alone and is not reproducible
from this repo — which is why it must stay committable rather than be swept up by
a blanket `*.dl` ignore rule.

## facts.duckdb is data, not a cache

`facts.duckdb` owns the logical fact terms. The `facts.subject/relation/object`
columns in `kb.sqlite` are **display mirrors**, and rendering a term into text is
lossy: the compound `Compound('person', (StringLit('Ada'),))` renders as the text
`person("Ada")`, which is indistinguishable from a plain string that happens to
look like a compound. Rebuilding the terms from those mirrors would silently
reinterpret structure as text, so verinote refuses to do it.

Lose the sidecar and the engine stops instead of guessing:

```text
DuckDBFactTermStoreError: missing DuckDB fact terms for fact id(s): 1.
Restore facts.duckdb from backup or re-enter the affected facts. Refusing to
rebuild them from SQLite display text because that would reinterpret
structural terms as strings.
```

That refusal is the resolution of
[#156](https://github.com/semantic-reasoning/verinote/issues/156). Before it, a
lost sidecar was silently re-typed — compounds collapsed into strings, rules that
matched them stopped firing, and the report still called the knowledge base
consistent. **The failure is loud now, but it is still not recoverable:** verinote
can tell you the terms are gone; it cannot bring them back. Only your backup can.

The one exception is a legacy KB whose facts predate the sidecar entirely (no
fact-terms marker recorded in `kb.sqlite`). Those rows are backfilled as
`StringLit` values the first time they are selected for verification, because
there is no structure to lose. Once any fact has been written through the sidecar,
the marker exists and the refusal above applies.

**Back the sidecar up with the rest of the KB.**

## Keep the KB outside the working tree

The default root (`./data`) is a convenience for a first run, not a safe home:

```bash
VERINOTE_ROOT=~/verinote-kb verinote init   # scaffold a KB outside the repo
VERINOTE_ROOT=~/verinote-kb verinote ui
```

`VERINOTE_ROOT` selects the KB root for every command, so exporting it once in
your shell profile keeps all of your data out of the working tree.

### A KB inside the repo tree gets committed

If you keep a KB at any path other than `data/` — say `./my-kb` — a stray
`git add -A` will commit most of it. Only the generated artifacts are ignored; the
sources are not, and that is deliberate. The ignore rules match generated *paths*,
never bare extensions, because a blanket `*.dl` rule is exactly what used to
swallow hand-written policy. What gets staged:

```text
my-kb/sources/confidential.pdf        <- the document you ingested, byte for byte
my-kb/artifacts/sources/1/<sha>.txt   <- its extracted text
my-kb/policy/logic-policy.dl          <- your review rules
my-kb/config.json                     <- your provider/model settings
```

So the exposure is not one policy file — it is **the documents themselves**, which
is exactly what [AGENTS.md](../AGENTS.md) forbids committing. Keep the KB outside
the tree, or do not blind-add.

### `git clean -fdx` deletes your KB

Ignoring it does not save it: `-x` removes ignored files too, and user data cannot
be committed to fix that. (Without `-x`, ignoring *does* protect the generated
artifacts — but not `sources/` or `policy/`, which are not ignored.) Running it
inside the working tree destroys the KB and its audit log with no undo.

Moving the default root outside the working tree is tracked in
[#185](https://github.com/semantic-reasoning/verinote/issues/185).

## Backups are your responsibility

verinote takes none. Copy **the whole KB root** — it is a plain folder — on your
own schedule:

```bash
cp -a ~/verinote-kb ~/backups/verinote-kb-$(date +%F)
```

Do not snapshot only part of it. `kb.sqlite` alone is not a backup: without
`facts.duckdb` the engine refuses to run at all (see above), and without
`sources/` and `artifacts/` the provenance behind every confirmed fact is gone.
