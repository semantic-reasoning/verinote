# Configuration

## The active KB

On first launch, if verinote cannot find an active KB, the web app opens a KB
selection screen. Choose a KB folder there; if the folder has no `kb.sqlite`,
verinote creates one. On later launches, the app opens that KB directly.

The active KB path is saved in a platform-native app config file:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\verinote\app.json` |
| macOS | `~/Library/Application Support/verinote/app.json` |
| Linux/Unix | `${XDG_CONFIG_HOME:-~/.config}/verinote/app.json` |

`VERINOTE_ROOT` overrides the saved active KB and is still useful for scripts,
tests, and one-off launches:

```bash
VERINOTE_ROOT=/path/to/kb verinote ui
```

## Scaffolding a KB from the CLI

`init` and `seed` are *local* commands: they never write to the saved active KB.
They target the root you name, else `VERINOTE_ROOT`, else `./data` in the current
directory.

```bash
verinote init                 # ./data here (or $VERINOTE_ROOT if set)
verinote init /path/to/kb     # a named root
verinote seed /path/to/kb     # demo facts into an existing KB
```

Creating a KB does not make it the active one. Every other command still reads the
saved active KB, so to work with the KB you just created, either point
`VERINOTE_ROOT` at it or select it in the UI:

```bash
VERINOTE_ROOT=/path/to/kb verinote status
```

Seeded demo facts land as `candidate`/`needs_review`, never as engine input — demo
data has to pass through human review like anything else.

> Prefer a KB outside the working tree. See
> [operations.md](operations.md#keep-the-kb-outside-the-working-tree).

## Providers

Provider choice lives in `config.json` (or `VERINOTE_PROVIDER`), and one adapter
is selected from it: `anthropic`, `claudecli`, `openai`, or `ollama`. Install only
the SDK you need — the LLM extras exist so the app installs without any single
vendor's package:

```bash
pip install -e ".[anthropic]"   # or .[openai] — Ollama needs no SDK
```

## Auto-accept

`auto_accept_recommendations` is the one setting that changes what verinote
promises. It is **off by default**. With it on, extraction is followed by a rule
(`corroborated_no_conflict`) that promotes eligible review-tier facts straight to
`accepted` — an engine status — recorded with `actor="rule"` instead of a human
click.

The gate is still there: the rule only fires on facts that are corroborated and
conflict-free, every promotion lands in the audit log, and you can still supersede
anything it accepted. But while it is on, **"no fact reaches the engine without a
human looking at it" is no longer true of your KB.** Turn it on when you trust the
rule more than you value the click; leave it off if the audit trail must show a
person behind every accepted fact.

Set it in the Settings UI, in `config.json`, or via
`VERINOTE_AUTO_ACCEPT_RECOMMENDATIONS`.

## Optional extras

| Extra | What it installs |
|---|---|
| `anthropic`, `openai` | the vendor SDK for that provider |
| `ingest` | `python-docx` + `pypdf`, for binary source ingestion (docx/pdf → text) |
| `test` | the test dependencies |
| `analytics` | nothing — a **compatibility no-op**. DuckDB is a core dependency because it powers verification, and analytics uses that same dependency. |
| `wirelog` | the legacy `pyrewire` path, for compatibility/debugging only |
