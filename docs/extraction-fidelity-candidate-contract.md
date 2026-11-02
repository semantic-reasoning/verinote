# Evidence-link candidate trial contract

This is a proposal for one separately approved trial, not authorization to make
model calls. The opt-in `evidence_links_v1` candidate adds generic source-evidence
link checks to the production v2 extraction instructions. The compiler inspects
only approved relation IDs; it retains the exact production input JSON bytes.
It receives no fixture annotations, gold answers, historical output or diagnosis.
The same production validators and finite evaluator remain unchanged.

The trial uses one fresh Store and one new Claude extraction owner for the full
v2 `three` fixture. It makes three sequential initial requests and at most two
existing quote-repair requests, never more than one repair per chunk. No resume,
replacement owner, model fallback or automatic retry is allowed. Request `sonnet`
and require resolved identity `claude-sonnet-5-5`, observed in the baseline.
Unknown or changed model/worker identity stops admission and prevents acceptance.
A trial-only manager owns the lease lock and worker construction path. Its
permanent creation-attempted latch is set before the first constructor call;
constructor failure, retirement, disappearance, death, stopped state or key
change can never permit another construction. Healthy reuse retains that exact
worker. External death after the health check may fail the current exchange,
but cannot trigger a replacement. The guard is installed only on a fresh,
unexposed one-slot pool; displaced virgin managers are closed. Guard failures
are infrastructure uncertainty and therefore inconclusive.

## Proposed budget

- At most **5 provider requests**, each with timeout at most **90 seconds**.
- **600 seconds** observed wall time, including setup, cleanup, raw hashing and
  offline-quality verification; final report/index/evaluation writes follow the
  last wall observation and are explicitly disclosed in the report.
- **300,000 observed input + output tokens**. Cache counts remain separate usage
  evidence. This is neither a guaranteed token/spend cap nor a cost estimate;
  an already dispatched request can exceed the observed admission threshold.

The baseline three-chunk run generated 43,037 observed tokens in 30.91 seconds;
its prompts were approximately 23.8 KB each. The proposed token allowance includes
the candidate addition and up to two repairs. Five 90-second requests account
for 450 seconds, leaving 150 seconds for processing and verification. No request
is admitted after a terminal failure, unknown usage or reached budget.

## Commands and identity

```sh
PYTHONPATH=. python scripts/evaluate_extraction_fidelity.py plan
PYTHONPATH=. python scripts/evaluate_extraction_fidelity.py evaluate /path/to/artifacts
# Only after separate user approval of this reviewed proposal:
PYTHONPATH=. python scripts/evaluate_extraction_fidelity.py run --output /new/path --allow-model-calls
```

The real run requires a clean committed checkout. Before generation it retains
recursive Python source copies (including diagnostics), the script and contract,
fixture/gold manifest, production/repair/candidate prompts and hashes, compiler
hash, fixed plan, limits, CLI version/binary hash, revision/tree, invocation,
requested model and transport options. Each call retains the original and actual
prompt plus a candidate sidecar joined to the manifest, run, chunk, repair flag
and provider call ID. Initial extraction is compiled; quote-repair prompts are
unchanged. The baseline-format checkpoint alone does not establish candidate
identity. The new verifier joins it to this manifest and every raw response.

The existing production Service performs extraction, accounting normalization,
quote repair, checkpointing and pending publication. Offline verification uses
the existing DB/quality/usage/timing verifier and additionally reconstructs exact
prompts, mechanical index audits, quote-repair responses/audits, all normalized
checkpoint outputs, and absence of extra calls or resumed chunks. Retained code
is never executed. No HTTP, subprocess or writable DB is used in evaluation.
Saved verdicts are ignored. Current implementation and retained source hashes
must match. Any unknown, missing, changed, unaccounted or corrupt evidence makes
the result inconclusive. A pending finalization marker prevents an interrupted
or failed final write from publishing an accepted result.

`accepted_for_further_testing` requires a complete real run, known linked usage
and identity, all production/finite-gold/safety checks passing, and intact
budgets/cleanup/recording. An authenticated production, finite-gold or publication
failure is `rejected` only when no infrastructure/limit uncertainty overrides it.
Other cases are `inconclusive`. Injected owners always label the entire trial
synthetic and can never yield real acceptance or rejection. This single finite
fixture cannot establish general fidelity, speedup or operational acceptance.
