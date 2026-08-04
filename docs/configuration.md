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

The same app-level file stores the Settings theme preference: `system` (the
default, following the operating system), `light`, or `dark`. It is independent
of the active KB, so changing KBs does not change the selected theme. Theme
changes are intentionally unavailable while a KB's logic policy is halted: the
policy guard permits only writes that leave the halted KB.

`VERINOTE_ROOT` overrides the saved active KB for the UI and is still useful for
scripts, tests, and one-off launches:

```bash
VERINOTE_ROOT=/path/to/kb verinote ui
```

## CLI KB roots

Every non-UI CLI command uses the same root resolver. Precedence is `--root`,
then `VERINOTE_ROOT`, then a CWD-independent platform user-data default:

| Platform | Default KB root |
|---|---|
| Windows | `%LOCALAPPDATA%\\verinote\\kb` |
| macOS | `~/Library/Application Support/verinote/kb` |
| Linux/Unix | `${XDG_DATA_HOME:-~/.local/share}/verinote/kb` |

Explicit roots expand `~` but must be absolute; relative roots are rejected.
`init` and `seed` retain an absolute positional root as a temporary compatibility
alias, but it cannot be combined with `--root`.

```bash
verinote init --root /path/to/kb
verinote --root /path/to/kb status
VERINOTE_ROOT=/path/to/kb verinote seed
verinote init /path/to/kb     # temporary positional compatibility alias
```

Creating a KB does not make it the active UI selection. The web UI still uses its
saved active KB unless `VERINOTE_ROOT` or its own `--root` is provided. CLI
commands continue to use the resolver above:

```bash
VERINOTE_ROOT=/path/to/kb verinote status
```

Seeded demo facts land as `candidate`/`needs_review`, never as engine input — demo
data has to pass through human review like anything else.

> Prefer a KB outside the working tree. See
> [operations.md](operations.md#keep-the-kb-outside-the-working-tree).

`init` and `seed` refuse targets inside normal and linked Git worktrees before
creating any KB files, including nested or symlinked paths that resolve there.

## Providers

Provider choice lives in `config.json` (or `VERINOTE_PROVIDER`), and one adapter
is selected from it: `anthropic`, `claudecli`, `openai`, or `ollama`. Install only
the SDK you need — the LLM extras exist so the app installs without any single
vendor's package:

```bash
pip install -e ".[anthropic]"   # or .[openai] — Ollama needs no SDK
```

### Picking an Ollama model

With `ollama` selected, Settings turns the Model field into a picker over the
models that server actually has (`GET /api/tags` against the Base URL you
configured, or `http://localhost:11434` when it is unset), so a mistyped tag
cannot become a runtime failure at the first extraction. The list reloads when
you change the provider or the Base URL, and a **Refresh list** button picks up
anything pulled since the page loaded.

Three outcomes stay distinct, because the fix for each is different:

| What you see | What it means |
|---|---|
| A picker | that server is reachable and has these models |
| Text input + *"has no models installed"* | reachable, but nothing pulled yet — `ollama pull <model>` |
| Text input + *"Could not load the model list"* | that endpoint could not be reached; the error is quoted verbatim |

The field never becomes unusable: when the list cannot be loaded you can still
type a model id and save. And a model named in `config.json` that the server
does not have stays selected, marked `— not installed`, rather than being
silently swapped for one that is — the page reports your KB's real state.

### Picking a Claude CLI model

The `claude` binary has no listing command, so there is nothing to discover —
its `--model` help documents three aliases and otherwise takes a full model id.
Settings offers those as a dropdown:

| Option | Meaning |
|---|---|
| **CLI default (no --model)** | verinote passes no `--model`, so the CLI uses whatever it is configured for |
| `fable` / `opus` / `sonnet` | the latest model of that family |
| *(a pinned id, when one is saved)* | shown selected, so the list always reports what `config.json` says |

Because that list is curated rather than discovered, it is not the set of models
you can reach — it is the set of aliases *verinote* resolves, which is a claim
about this code and not about your account. **Enter a model id** swaps the
dropdown for a text box when you want to pin a version: a full id such as
`claude-opus-4-8` reaches the CLI unchanged and stays on that version. A model
that does not exist, or that your CLI cannot reach, surfaces as the CLI's own
error rather than a silent substitution.

Because the list cannot be verified against a server, **Test connection** is the
check that matters here: it runs one real extraction through the `claude` binary
with the model you chose.

The cloud providers keep the plain free-text field — a vendor catalogue is not a
property of the endpoint you point at, so there is no list they could answer
truthfully either.

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
