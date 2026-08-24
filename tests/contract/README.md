<!-- SPDX-License-Identifier: MPL-2.0 -->
# Provider contract tests (issue #241)

These tests exercise failures the deterministic suite could not see when each was
written: output from a **real LLM provider** it stubs, an upstream API it never
calls, and a sync exit code it did not then check.

| Guard | Issue | What it locks |
|-------|-------|---------------|
| `test_query_intent_contract.py` | #237 | A role question the deterministic parser hands off must yield a valid intent through the live provider and the production parse boundary. |
| `test_extraction_contract.py` | #238 | A founding-date fact the extractor produces must normalise into the policy's *functional* relation vocabulary, so a two-date contradiction is catchable. |
| `test_sync_rc_contract.py` | #239 | `verinote sync` must not report success when every extraction chunk fails. Drives the real CLI with a stub client, so it needs no provider; runs in the default suite since #469. |
| `test_openrouter_catalogue_contract.py` | — | OpenRouter's model catalogue must still carry the `id` and `supported_parameters` fields the settings Model picker is built from, and still declare `structured_outputs`. Needs no key or client; reads the live endpoint. |
| `test_contract_meta.py` | — | Meta guards on the harness itself (marker registered, fixtures carry provenance, every module here but this one declares a guard or is on record as promoted, the skipped-run guard bites, the guards on the promotion ledger stayed promoted). Runs in the default suite. |

The rows are modules, not gating classes: `@pytest.mark.contract` is applied per
test, so a module here can hold both an opt-in guard and a test that runs in the
default suite. **Running** below says which is which.

## Running

The guards carrying `@pytest.mark.contract` are **opt-in**: they self-skip unless
you name a provider. The default `pytest tests` therefore runs the meta tests,
the deterministic precondition control in `test_query_intent_contract.py`, the
replay guards (issue #270), and the two guards issue #469 promoted — the DuckDB
functional-conflict control in `test_extraction_contract.py` and the #239 sync
exit-code guard in `test_sync_rc_contract.py` — and never reaches a provider.
Any invocation path works for the opt-in set:

```bash
VN_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
# or, equivalently:
VN_CONTRACT_PROVIDER=claudecli python3 -m pytest tests/contract -m contract -rs
VN_CONTRACT_PROVIDER=claudecli python3 -m pytest -m contract -rs
```

`run.sh` picks `python3`, then `python`. Point it at a specific interpreter with
`PYTHON` when the one it would find is not the one holding pytest and verinote's
dependencies — a checkout whose virtualenv lives elsewhere, for instance:

```bash
PYTHON=/path/to/.venv/bin/python VN_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
```

Two rules keep a green run from meaning nothing:

* **Asked for but all skipped ⇒ the session fails.** If a run asks for these
  guards and not one of them executes, `pytest_sessionfinish` in `conftest.py`
  turns it red. A fully-skipped opt-in run is a silent no-op, not a pass.
  Asking means any of the spellings pytest offers: `-m contract`, `-k contract`,
  or naming a path in this directory (`pytest tests/contract`, including
  `--pyargs tests.contract`). The default suite asks in none of those ways —
  `pytest` and `pytest tests` both target `tests`, a parent of this directory —
  so it is unaffected and the marked guards keep self-skipping there.

  Asking is not the same as failing: a run that *excludes* the guards on purpose
  (`pytest tests/contract -k meta`, `-m "not contract"`, `--deselect`) is silent,
  because the count is taken after deselection. `--collect-only` is exempt too,
  since not running tests is what it was asked to do.
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
    VN_CONTRACT_API_KEY="$OPENAI_API_KEY" python -m pytest tests/contract -m contract -rs
```

## Replay fixtures

`tests/fixtures/contract/<provider>/*.json` hold **pre-parse** provider responses
captured from a real provider (`captured_at` records when). The replay tests feed
the raw string or structured object back through the production parse boundary
(`parse_query_intent` / `parse_facts`), so they reproduce a captured failure
deterministically without a provider. Each provider directory must contain the
query-intent and extraction pair. The deterministic
`sync_all_chunks_failed.json` artifact is the only permitted flat fixture.

Since issue #270 the replays carry no marker and no gate, so `pytest tests` runs
them. To run just those nodes, set nothing at all:

```bash
.venv/bin/pytest -q \
    tests/contract/test_query_intent_contract.py::test_replay_raw_intent_parses_through_production_boundary \
    tests/contract/test_query_intent_contract.py::test_claudecli_replay_retains_reason_regression_shape \
    tests/contract/test_extraction_contract.py::test_replay_founding_relation_normalizes_into_functional_vocab
```

The parametrized nodes discover every valid provider fixture pair, so how many
tests those three targets collect follows the fixtures on disk rather than
anything written here. `test_contract_meta.py` runs the same three targets in a
child process with the gate unset and re-derives the expected count from the
same fixtures, discovered independently, so a replay that quietly takes the gate
back skips there and reddens the meta suite. A separate static guard reads the
source for the marker, for the gate fixture parameter, and for the guard's
disappearance; that one is not replay-scoped — it covers every guard listed in
`PROMOTED_GUARDS`, which since issue #469 includes guards that were never
replays.

The capture script sends only the fixed synthetic Acme Robotics question and
source text in `capture.py`. Do not change those inputs to customer, company,
person, document, or source data. It stages both live payloads before writing
the provider-qualified pair, so a failed capture cannot leave one new live
fixture behind.

Recapture with the repository virtual environment. The provider credentials must
already be available in your environment; never paste a credential into a
fixture, command history, or repository file:

```bash
VN_CONTRACT_PROVIDER=claudecli PYTHONPATH=$PWD \
    .venv/bin/python tests/contract/capture.py

VN_CONTRACT_PROVIDER=ollama VN_CONTRACT_MODEL=qwen3:8b \
    VN_CONTRACT_BASE_URL=http://localhost:11434 PYTHONPATH=$PWD \
    .venv/bin/python tests/contract/capture.py

VN_CONTRACT_PROVIDER=openai VN_CONTRACT_MODEL=gpt-4o \
    VN_CONTRACT_API_KEY="$OPENAI_API_KEY" PYTHONPATH=$PWD \
    .venv/bin/python tests/contract/capture.py

VN_CONTRACT_PROVIDER=anthropic VN_CONTRACT_MODEL=claude-opus-4-8 \
    VN_CONTRACT_API_KEY="$ANTHROPIC_API_KEY" PYTHONPATH=$PWD \
    .venv/bin/python tests/contract/capture.py
```

`claudecli` needs the `claude` executable on `PATH`. `ollama` needs a running
server at `VN_CONTRACT_BASE_URL` (default `http://localhost:11434`). `openai`
and `anthropic` require `VN_CONTRACT_API_KEY`; the commands above forward an
already-exported provider-specific environment variable. `VN_CONTRACT_MODEL`
and `VN_CONTRACT_BASE_URL` are honored by capture for every adapter that uses
them. The `#239` fixture is provider-free and is regenerated from the real
pipeline on every run.
