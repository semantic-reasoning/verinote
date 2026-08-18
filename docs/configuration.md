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
is selected from it: `anthropic`, `claudecli`, `openai`, `openrouter`, or
`ollama`. Install only the SDK you need — the LLM extras exist so the app
installs without any single vendor's package:

```bash
pip install -e ".[anthropic]"   # or .[openai] — Ollama needs no SDK
```

OpenRouter speaks the OpenAI wire protocol and inherits that adapter's request
paths, so it uses the same `.[openai]` extra; there is no separate `openrouter`
extra. What it does not inherit is the endpoint: an unset Base URL resolves to
`https://openrouter.ai/api/v1` rather than to OpenAI's API, so clearing the field
cannot send your documents to a vendor you did not select. A Base URL you do set
still overrides it.

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

### Picking an OpenRouter model

With `openrouter` selected, Settings turns the Model field into a picker too, but
over a different kind of list: the catalogue the endpoint you configured
publishes (`GET {base_url}/models` against your Base URL, or
`https://openrouter.ai/api/v1` when it is unset). That request carries **no API
key**, so what comes back is the catalogue that endpoint publishes — not a list
of what your account can reach. A model in the dropdown is one the catalogue lists,
not one verinote has confirmed your key can call: **Test connection** runs one
real extraction with the provider and model you chose, and that is what confirms
a choice works.

The options are split into two groups, **Advertises structured output** and
**Does not advertise structured output**, built from what each catalogue entry
declares in its `supported_parameters`. The split matters because verinote asks
for a JSON-schema `response_format` on every call that has to come back as JSON —
extraction, query translation, query intent — so picking from the second group
means asking a model for something its own entry does not advertise. Both groups
render even when one is empty, and neither is a measurement: verinote has not run
these models, it is repeating what the catalogue says about them.

As with Ollama, the list reloads when you change the provider or the Base URL, a
**Refresh list** button re-reads it, and the field never becomes unusable — when
the catalogue cannot be loaded you can still type a model id and save. The same
three outcomes stay distinct:

| What you see | What it means |
|---|---|
| A grouped picker | that endpoint is reachable and lists these models |
| Text input + *"is reachable but listed no models"* | reachable, but the catalogue came back empty |
| Text input + *"Could not load the model list"* | that endpoint could not be reached; the error is quoted verbatim |

A model named in `config.json` that the catalogue does not list stays selected,
marked `— not in this catalogue`, rather than being silently swapped — the page
reports your KB's real state. Leave the model blank and the built-in default
applies: `openai/gpt-oss-20b:free`, a concrete free model rather than the
`openrouter/free` router. A router picks a different model per request, so one
**Test connection** could not stand for the next call, and the settings banner
would name a model that did not answer. This one was chosen because every
endpoint serving it advertises structured output — which holds because it has
exactly one endpoint, not because a fleet of them was surveyed. And again,
*advertises*: whether an endpoint honours `strict` is only knowable by running
it.

Switching the provider select **to** OpenRouter clears the Base URL field,
because that field's only job is to point verinote at a different endpoint and
the endpoint you are leaving belongs to the provider you are leaving —
`http://localhost:11434` is not an OpenRouter endpoint. Only OpenRouter does
this. The clear is announced, not silent: a note names the value that was
discarded and what would be dialled instead, nothing is written until you press
**Save**, and typing the old value back in keeps it.

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

Anthropic and OpenAI keep the plain free-text field — a vendor's own catalogue is
not a property of the endpoint you point at, so there is no list they could
answer truthfully either. OpenRouter is the exception among the cloud providers,
and it does not generalise: there, the endpoint you are configuring is itself
what serves the catalogue.

## API keys

`anthropic`, `openai`, and `openrouter` authenticate with an API key. The other
two never read one at all — `claudecli` shells out to the `claude` binary and
`ollama` talks to a local endpoint — so Settings lists them as *no API key
needed*.

Settings has an **API keys** section with one small form per key-using provider —
its own form, so a key can never ride along with a provider/model save. All five
providers get a row; the two that need no key get no form. The field is a
password input and is never rendered back with a value, so submitting it empty
means *leave the current key alone*; **Remove saved key** is the separate action
that clears one.

Saved keys are written to `credentials.json` in the same app config directory as
`app.json` — never inside a KB, so a KB stays safe to copy or share — with file
mode `0600`, and each provider's key is stored separately so a key saved for one
is never sent to another. One caveat to that first claim: a key shorter than
the app's redaction threshold is accepted (a self-hosted gateway's token can
legitimately be that short) but Settings warns when you save one, because
redaction of a provider's error text only covers secrets at least that long,
and those error messages *are* stored in the KB.

A provider-scoped environment variable wins. For each provider, the key is
resolved in this order:

| Source | Notes |
|---|---|
| `VERINOTE_<PROVIDER>_API_KEY` | provider-scoped, e.g. `VERINOTE_OPENROUTER_API_KEY`, `VERINOTE_ANTHROPIC_API_KEY`, `VERINOTE_OPENAI_API_KEY` |
| the key saved in Settings | per provider |
| `VERINOTE_API_KEY` | legacy, provider-agnostic — applies to whichever provider got this far |

The saved key sits above the legacy variable on purpose: `VERINOTE_API_KEY` names
no provider, so ranking it first would take a key you saved *for OpenAI* and
replace it with whatever single value happens to be exported. The scoped variable
keeps env-first available without that ambiguity. Settings shows which of these
each provider's key is actually coming from, and warns when a saved key is being
shadowed by the environment.

An unreadable `credentials.json` is a halt, not an absence. If the file is there
but cannot be read or understood, and it is what would have decided the selected
provider's key, verinote refuses provider calls. That check sits at the one
place every provider call is built, before the provider is even chosen, so it
covers every path that calls one by construction — extraction, question repair,
asking a question, translating questions, the model list, and **Test
connection** are examples of what stops, not the full set — rather than
proceeding as though no key were saved and calling a provider unauthenticated. A
provider whose key comes from either environment variable is unaffected, because
the file could not have changed the outcome. Settings says which of those two
situations you are in, and `/credentials-unavailable` names the file and the
ways out — restore it from a backup, delete it, export the scoped variable for
the provider you are using, or switch to a provider that needs no key. Saving a
key stays refused until then: a save merges into the other providers' entries,
so writing over a file it cannot read would silently discard them.

verinote does not fall back to a vendor SDK's own `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`: a request that authenticated with a credential verinote
never resolved could not be redacted from an error message, so a missing key is
an error instead.

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

## Logic policy vocabulary

`verinote init` writes the shipped default policy to
`<root>/policy/logic-policy.dl`. That copy is yours: the engine re-checks every
fact against whatever it says, so the policy file is the one place review rules
live. Four predicates make up the vocabulary a KB declares there.

| Predicate | Kind | Active by default | What it says |
|---|---|---|---|
| `functional(rel)` | declared | yes | this relation holds at most one object per subject |
| `subclass_of(sub, super)` | declared | no | every `sub` is also a `super` |
| `domain_of(rel, cls)` | declared | no | using this relation as a subject makes you a `cls` |
| `is_a(entity, cls)` | **derived** | no | what the engine concludes from the three above |

Only `functional` runs out of the box. The three class predicates ship as a
commented-out block you switch on — see
[The block ships switched off](#the-block-ships-switched-off).

### `functional`

```
functional("established_on").
```

Two different objects for one subject on a functional relation is a blocking
`error_functional_conflict`. The shipped policy declares `established_on`,
`born_on`, and `died_on`; add and remove lines freely.

### `subclass_of` and `domain_of`

These say what a *subject* is, rather than how a relation behaves. Declare that
`Person` and `Organization` are both `Party` and one rule written about `Party`
covers both — and a third kind of Party is one new line, not an edit to every
rule that enumerated the kinds by hand.

```
subclass_of("Person", "Party").
subclass_of("Organization", "Party").
domain_of("hasSubscription", "Party").
```

These are the examples your policy file ships, commented out; replace them with
your own vocabulary when you enable the block.

**This lives in the policy file and not in the `facts` table**, and that is not
an implementation convenience. A `subclass_of` line is not an observation
extracted from a document — it is your declaration of what your words mean.
Routing it through the review gate would ask a person to approve their own
vocabulary as though an LLM had proposed it, and would demand a source document
for a statement no source ever made. `functional("established_on")` is in the
policy file for exactly this reason; these are the same kind of statement.

### `is_a`, and how far it reaches

`is_a` is derived, never written by hand. The block derives it four ways:

```
is_a(E, C) :- relation(E, "is_a", C).                                        // stated directly
is_a(E, S) :- relation(E, "is_a", C), subclass_of(C, S).                     // one superclass hop
is_a(S, C) :- relation(S, R, O), domain_of(R, C).                            // from using a relation
is_a(S, P) :- relation(S, R, O), domain_of(R, C), subclass_of(C, P).         // ...and one hop from there
```

**`is_a` is for rules in this file, not for questions.** A query may only
reference `relation/3`, so asking about `is_a` from the Ask box is rejected with
`unknown predicate: is_a`. The way you use it is a policy rule:

```
.decl warn_party_without_subscription(entity: symbol)
warn_party_without_subscription(E) :- is_a(E, "Party"), ...
```

That is the payoff the vocabulary exists for: one rule, written about `Party`
alone, that reaches every kind of Party without naming them.

**The ceiling is one superclass hop, on both paths.** The DuckDB backend refuses
recursive rules, so the hierarchy reaches exactly as far as the rules spell out
and no further. Declare `Person` → `Party` → `Agent` and a Person derives
`Person` and `Party` but **not** `Agent` — silently, with no warning, because a
missing derivation is indistinguishable from one you never wanted.

A third level needs **both** of these, one per derivation path. Adding only the
first leaves anything classified through `domain_of` a level short, which is the
silent mismatch the fourth rule above exists to avoid:

```
is_a(E, T) :- relation(E, "is_a", C), subclass_of(C, S), subclass_of(S, T).
is_a(S, T) :- relation(S, R, O), domain_of(R, C), subclass_of(C, S2), subclass_of(S2, T).
```

### The block ships switched off

**Everything in this section ships commented out** — the `.decl` lines and the
four rules as well as the examples. Your copy carries the block as text with a
`TO ENABLE` note; uncomment every line between its `BEGIN` and `END` markers to
switch it on, then replace the example vocabulary with your own.

It is off by default because the default policy is not scaffolding-only: a KB
that never recorded a policy of its own is verified against it at runtime. A live
`subclass_of("Person", "Party")` would make an untouched KB that says only *Ada
is_a Person* start deriving that Ada is a Party, a class nobody declared —
harmless if your "Party" means what ours does, wrong and silent if it is a
political party. And a live `is_a` rule names the `is_a` relation in its body, so
every KB without a hierarchy would carry a dead-rule warning about a rule its
owner never wrote. A new KB's first check is quiet, and this keeps it that way.

Once enabled, note the difference in how the two declarations are policed:

- a live `domain_of` naming a relation your KB has no fact for is reported as a
  dead rule, the same as an unused `functional`
  ([#245](https://github.com/semantic-reasoning/verinote/issues/245)). The
  shipped `domain_of("hasSubscription", "Party")` example does exactly this until
  you replace it;
- a live `subclass_of` naming a class nothing uses is **not** reported. Dead-rule
  detection reads columns named `rel`, and `subclass_of`'s columns are `sub` and
  `super`, so a misspelled or obsolete `subclass_of` line is silent. Nothing will
  tell you about it.

### The `is_a` warning you will see once it is on

With the block enabled, a KB whose facts never use the `is_a` relation reports
this on every check:

```
dead_rule: policy declares relation("is_a") but no engine fact uses that relation
```

This is expected output, not a bug. It means the class machinery is inert for
this KB. Editing or deleting the examples will not clear it — the rule bodies
themselves name the relation. To clear it, comment the `is_a` rules back out, the
same move as deleting a `functional("born_on")` your KB never uses.

## Optional extras

| Extra | What it installs |
|---|---|
| `anthropic`, `openai` | the vendor SDK for that provider (OpenRouter uses `openai` — see [Providers](#providers)) |
| `ingest` | `python-docx` + `pypdf`, for binary source ingestion (docx/pdf → text) |
| `test` | the test dependencies |
| `analytics` | nothing — a **compatibility no-op**. DuckDB is a core dependency because it powers verification, and analytics uses that same dependency. |
| `wirelog` | the legacy `pyrewire` path, for compatibility/debugging only |
