<!-- SPDX-License-Identifier: MPL-2.0 -->
# Provider contract tests (issue #241)

These tests exercise failures that only surface against a **real LLM provider**,
or that the deterministic suite would otherwise paper over:

| Guard | Issue | What it locks |
|-------|-------|---------------|
| `test_query_intent_contract.py` | #237 | A role question the deterministic parser hands off must yield a valid intent through the live provider and the production parse boundary. |
| `test_extraction_contract.py` | #238 | A founding-date fact the extractor produces must normalise into the policy's *functional* relation vocabulary, so a two-date contradiction is catchable. |
| `test_sync_rc_contract.py` | #239 | `verinote sync` must not report success when every extraction chunk fails. |
| `test_openrouter_catalogue_contract.py` | — | OpenRouter's model catalogue must still carry the `id` and `supported_parameters` fields the settings Model picker is built from, and still declare `structured_outputs`. Needs no key or client; reads the live endpoint. |
| `test_contract_meta.py` | — | Meta guards on the harness itself (marker registered, fixtures carry provenance, every module has a guard, the skipped-run guard bites). Runs in the default suite. |

## Running

The guards are **opt-in**. They self-skip unless you name a provider, so the
default `pytest tests` stays green (only the meta tests and the deterministic
positive controls run there). Any invocation path works:

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
  so it is unaffected and the guards keep self-skipping there.

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
deterministically without a provider — while still gated opt-in so the default
suite stays green. Each provider directory must contain the query-intent and
extraction pair. The deterministic `sync_all_chunks_failed.json` artifact is the
only permitted flat fixture.

Run the captured query/extraction replays without a provider, credentials, or
network access with this exact command:

```bash
VN_CONTRACT_PROVIDER=replay .venv/bin/pytest -q \
    tests/contract/test_query_intent_contract.py::test_replay_raw_intent_parses_through_production_boundary \
    tests/contract/test_query_intent_contract.py::test_claudecli_replay_retains_reason_regression_shape \
    tests/contract/test_extraction_contract.py::test_replay_founding_relation_normalizes_into_functional_vocab
```

The parametrized nodes discover every valid provider fixture pair: the current
fixture layout runs 3 tests, and each additional provider pair adds 2 more.

`replay` is intentionally not a real provider. It only satisfies
`require_opt_in` for these deterministic tests; the explicit node IDs exclude
the live guards. If a live guard is added to this command by mistake, it fails
at provider validation before an adapter or network request is created.

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
