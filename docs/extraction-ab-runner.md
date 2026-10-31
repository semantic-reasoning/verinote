# Frozen extraction A/B runner

This runner implements the predeclared [contract v1](extraction-sonnet-baseline-completed.md#755에-적용할-사전-ab-판정-계약-v1).
It compares the same candidate revision's explicit Claude scheduler at concurrency
1 and 2. It does not enable concurrency in production or establish an improvement
without actual measurements. Synthetic tests cannot authorize adoption.

Print the fixed plan without any subprocess or model invocation:

```sh
.venv/bin/python scripts/benchmark_extraction_ab.py plan
```

The plan contains 70 measured documents and 15 warm-up documents. It fixes primary
long/boundary at ten pairs each, secondary short/dense/none at three pairs each,
and cold long/boundary at three pairs each. Schedules, seeds, 90-second configured
call timeout and decision thresholds are not CLI options. There are 137 initial
requests and at most 137 quotation repairs: the whole plan's request ceiling is
274. Smaller approved budgets can stop the run; they never reduce the required
sample size or authorize replacements.

After a **separate approval naming model, request, elapsed-time and observed-token
budgets**, run from a clean checkout. Supply the approved values explicitly:

```sh
.venv/bin/python scripts/benchmark_extraction_ab.py run \
  --output /absolute/new/result-directory --model sonnet \
  --max-requests APPROVED_REQUESTS --max-seconds APPROVED_SECONDS \
  --max-tokens APPROVED_OBSERVED_TOKENS --allow-model-calls
```

The output parent must already exist and contain no symlink components. This
example is a template, not authorization or a claim that arbitrary limits finish
the experiment. `274 × 90 = 24,660 seconds` is a conservative serial request-timeout
sum, not predicted elapsed time or an elapsed-time hard cap. Preparation, records,
evaluation and cleanup also take time. Usage is observed after responses, so
already admitted calls can cross the observed-token or elapsed-time threshold.
They are drained and counted; no new calls are authorized after a terminal stop.
Unknown comparison evidence (including usage, actual model or reported cost), model
changes, invalid worker lifecycle, document failure, recording failure or cleanup
error stops further calls.
There are no retries, replacement samples, extra warm-ups or partial-result cleanup.

Each primary/secondary case owns independent A/B pools for its entire series. The
runner targets each worker slot for exactly one short-document warm-up through
the production Service and the same global BudgetCaller. Slot views retain their
target across copies and timeout limits and cannot borrow another idle slot.
Subsequent warm calls must report reuse of the exact prepared pool/slot/worker/
generation. Unknown identity or restart makes that comparison inconclusive.

Every document uses a fresh synthetic Store, approved fixture dictionary and
checkpoint. Both the ordinary benchmark and A/B runner use the same document
execution helper and Service scheduler. Cold time begins before Store and owner
creation and ends immediately after evaluation and successful owner termination.
This boundary is identical for both arms. Per-document artifact hashing and later
durable report/index/verdict writes occur outside individual cold timings, while
the overall budget clock continues through those operations. Failed owner cleanup
retains elapsed time and costs but produces no valid cold timing. Warm primary/secondary
latency uses the raw Service submission-to-completion timing.

The runner records the clean Git revision/tree, CLI version obtained from its
version command, Python/platform, transport configuration, invocation, fixture and
prompt hashes, full plan and approved limits before the first request. Provider
cache state remains unknown. Only validated versioned Claude family identifiers
from the same call's usage `modelUsage` are exported as resolved models; aliases,
multiple-model markers and unrecognized identifiers are unknown. The evaluator
requires one consistent actual model, known usage and CLI-reported USD. Configured
estimates never substitute for reported USD or actual model identity.

## Retained evidence and offline evaluation

The new output directory retains `plan.json`, `environment.json`, `report.json`,
`index.json`, `evaluation.json` and `documents/<planned-key>/`. Each document keeps
its SQLite database, raw CLI/run/usage/timing records, validated checkpoint and
quality reports. No user document or user database is accepted as input. Per-row
attempt ranges and document keys link all initial/repair/warm-up/failed attempts.
Hashes detect missing, modified or conflicting artifacts; they do not authenticate
the machine or provider. Preserve partial directories after interruption or disk
failure, including those without a completed index.

```sh
.venv/bin/python scripts/benchmark_extraction_ab.py evaluate /absolute/result-directory
```

Evaluation launches no subprocess or model and never trusts a stored verdict. It
verifies the hash index, metadata and raw document/usage/timing linkage, recomputes
quality from retained validated results, and then applies the fixed contract.
Missing or unverifiable evidence is inconclusive. Only after a document’s raw
identity, summary, timing and calls agree, confirmed partial publication, altered
candidates or improper approval state are preserved as specific rejection reasons.
These safety violations and confirmed completed-document quality violations are
rejected even with missing later samples. Corrupt hashes or forged summaries do
not establish a confirmed violation. A valid complete
comparison's threshold failure remains rejected when another comparison is
incomplete; partial ratios alone cannot establish a threshold failure.

Ratios use paired B/A times, summed measured B/A reported cost and input+output
tokens. Primary additionally checks warm-up-inclusive series totals. Secondary
warm-up costs are disclosed separately. The deterministic primary bootstrap uses
20,000 resamples of ten paired ratios and the fixed linear percentile definition.
The evaluator keeps unknown/zero denominators unknown and never inserts epsilon.
`accepted` only allows the separately tracked operational-adoption review for the
measured model, fixtures and settings. #755 remains open until actual approved
measurements yield sufficient evidence for acceptance or rejection.
