<!-- SPDX-License-Identifier: MPL-2.0 -->
# Provider contract tests (issue #241)

These tests exercise failures that only surface against a **real LLM provider**,
or that the deterministic suite would otherwise paper over:

| Guard | Issue | What it locks |
|-------|-------|---------------|
| `test_query_intent_contract.py` | #237 | A role question the deterministic parser hands off must yield a valid intent through the live provider and the production parse boundary. |
| `test_extraction_contract.py` | #238 | A founding-date fact the extractor produces must normalise into the policy's *functional* relation vocabulary, so a two-date contradiction is catchable. |
| `test_sync_rc_contract.py` | #239 | `verinote sync` must not report success when every extraction chunk fails. |
| `test_contract_meta.py` | — | Meta guards on the harness itself (marker registered, fixtures carry provenance, every module has a guard, the skipped-run guard bites). Runs in the default suite. |

## Running

The guards are **opt-in**. They self-skip unless you name a provider, so the
default `pytest tests` stays green (only the meta tests and the deterministic
positive controls run there). Any invocation path works:

```bash
VN_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
# or, equivalently:
VN_CONTRACT_PROVIDER=claudecli python -m pytest tests/contract -m contract -rs
VN_CONTRACT_PROVIDER=claudecli python -m pytest -m contract -rs
```

Two rules keep a green run from meaning nothing:

* **Selected but all skipped ⇒ the session fails.** Whenever `-m` names the
  `contract` marker and not one selected test executes, `pytest_sessionfinish`
  in `conftest.py` turns the run red. A fully-skipped opt-in run is a silent
  no-op, not a pass. The default suite passes no `-m`, so it is unaffected.
* **A set gate pointing at an unreachable provider ⇒ fail, not skip** (issue
  #234). A provider you asked to exercise but that cannot run is a real gap.

## Providers

`VN_CONTRACT_PROVIDER` selects the adapter. Optional companions:

| Variable | Used by | Default |
|----------|---------|---------|
| `VN_CONTRACT_PROVIDER` | gate + client | (unset ⇒ skip) |
| `VN_CONTRACT_MODEL` | all providers | provider default |
| `VN_CONTRACT_BASE_URL` | ollama | `http://localhost:11434` |
| `VN_CONTRACT_API_KEY` | openai / anthropic | (unset ⇒ fail) |

The `VN_` prefix is load-bearing. The root `tests/conftest.py` sandbox drops
every `VERINOTE_*` variable at session start so an ambient export cannot change
what a test sees. A gate under that prefix would be erased before any fixture
could read it — which is why these live outside it and are simply read at
fixture time, from any invocation path, with no snapshot and no ordering race
(issue #272).

```bash
VN_CONTRACT_PROVIDER=ollama VN_CONTRACT_MODEL=qwen3:8b \
    python -m pytest tests/contract -m contract -rs

VN_CONTRACT_PROVIDER=openai VN_CONTRACT_MODEL=gpt-4o \
    VN_CONTRACT_API_KEY=sk-... python -m pytest tests/contract -m contract -rs
```

## Replay fixtures

`tests/fixtures/contract/*.json` hold **pre-parse** provider responses captured
from a real provider (`captured_at` records when). The replay tests feed the raw
string back through the production parse boundary (`parse_query_intent` /
`parse_facts`), so they reproduce a captured failure deterministically without a
provider — while still gated opt-in so the default suite stays green.

Recapture (needs a live provider) with:

```bash
VN_CONTRACT_PROVIDER=claudecli PYTHONPATH=$PWD \
    python tests/contract/capture.py
```

`capture.py` currently drives `claudecli`. To capture from another provider,
point its config at that adapter in `_live_config()` and supply the matching
`VN_CONTRACT_*` credentials; the `#239` fixture is provider-free and is
regenerated from the real pipeline on every run.
