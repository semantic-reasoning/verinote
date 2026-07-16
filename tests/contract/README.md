<!-- SPDX-License-Identifier: MPL-2.0 -->
# Provider contract tests (issue #241)

These tests exercise failures that only surface against a **real LLM provider**,
or that the deterministic suite would otherwise paper over:

| Guard | Issue | What it locks |
|-------|-------|---------------|
| `test_query_intent_contract.py` | #237 | A role question the deterministic parser hands off must yield a valid intent through the live provider and the production parse boundary. |
| `test_extraction_contract.py` | #238 | A founding-date fact the extractor produces must normalise into the policy's *functional* relation vocabulary, so a two-date contradiction is catchable. |
| `test_sync_rc_contract.py` | #239 | `verinote sync` must not report success when every extraction chunk fails. |
| `test_contract_meta.py` | — | Meta guards on the harness itself (marker registered, fixtures carry provenance, every module has a guard). Runs in the default suite. |

## Running

The guards are **opt-in**. They self-skip unless you name a provider, so the
default `pytest tests` stays green (only the meta tests and the deterministic
positive controls run there):

```bash
# opt-in run — must target tests/contract (see "Why target tests/contract")
VERINOTE_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
# or directly:
VERINOTE_CONTRACT_PROVIDER=claudecli python -m pytest tests/contract -m contract -rs
```

`run.sh` additionally **fails** if every collected contract test skipped — a
fully-skipped opt-in run is a silent no-op, not a pass.

A gate that is *set* but points at an unreachable provider (e.g. the `claude`
binary is missing) makes the tests **fail, not skip** (issue #234): a provider
you asked to exercise but that cannot run is a real gap in coverage.

### Why target `tests/contract`

The root `tests/conftest.py` strips every `VERINOTE_*` variable at session start.
`tests/contract/conftest.py` snapshots `VERINOTE_CONTRACT_PROVIDER` at import
time to beat that strip — which works when this directory is on pytest's direct
invocation path (`pytest tests/contract ...`). Discovering the whole tree from a
parent (`pytest tests`) loads this conftest only mid-collection, after the strip,
so the gate reads as unset and the guards skip. Always opt in by targeting
`tests/contract`.

This is a limitation of capturing the gate from a subdirectory conftest, not a
design choice: the root strip cannot be seen from here in time. Revisit if the
root `tests/conftest.py` sandbox is reworked to preserve `VERINOTE_CONTRACT_*`
(or to snapshot the gate itself), at which point the gate could be honored from
any invocation path and this caveat dropped.

## Providers

`VERINOTE_CONTRACT_PROVIDER` selects the adapter. Optional companions:

| Variable | Used by | Default |
|----------|---------|---------|
| `VERINOTE_CONTRACT_PROVIDER` | gate + client | (unset ⇒ skip) |
| `VERINOTE_CONTRACT_MODEL` | ollama / openai / anthropic | provider default |
| `VERINOTE_CONTRACT_BASE_URL` | ollama | `http://localhost:11434` |
| `VERINOTE_CONTRACT_API_KEY` | openai / anthropic | (unset ⇒ fail) |

These use a `VERINOTE_CONTRACT_*` namespace (not `VERINOTE_*`) precisely so the
root sandbox does not strip them.

```bash
VERINOTE_CONTRACT_PROVIDER=ollama VERINOTE_CONTRACT_MODEL=qwen3:8b \
    python -m pytest tests/contract -m contract -rs

VERINOTE_CONTRACT_PROVIDER=openai VERINOTE_CONTRACT_MODEL=gpt-4o \
    VERINOTE_CONTRACT_API_KEY=sk-... python -m pytest tests/contract -m contract -rs
```

## Replay fixtures

`tests/fixtures/contract/*.json` hold **pre-parse** provider responses captured
from a real provider (`captured_at` records when). The replay tests feed the raw
string back through the production parse boundary (`parse_query_intent` /
`parse_facts`), so they reproduce a captured failure deterministically without a
provider — while still gated opt-in so the default suite stays green.

Recapture (needs a live provider) with:

```bash
VERINOTE_CONTRACT_PROVIDER=claudecli PYTHONPATH=$PWD \
    python tests/contract/capture.py
```

`capture.py` currently drives `claudecli`. To capture from another provider,
point its config at that adapter in `_live_config()` and supply the matching
`VERINOTE_CONTRACT_*` credentials; the `#239` fixture is provider-free and is
regenerated from the real pipeline on every run.
