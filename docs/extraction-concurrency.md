# Bounded extraction experiment

The default extraction path remains sequential. `Service.start(...,
extraction_concurrency=1|2)` opts a Claude extraction run into the same bounded
scheduler with one or two independent workers. Dictionary generation, other
providers and the global document queue are unchanged. The HTTP API does not
expose this experimental option.

The coordinator alone validates responses in source order, performs at most one
quotation repair per chunk and two per document, and saves a contiguous validated
checkpoint prefix. Running calls plus completed responses awaiting acceptance
never exceed the chosen concurrency. The deadline begins at initial dispatch and
covers worker admission, request, ordering, repair and validation. A worker error
closes admission for both new initial calls and repairs; previously admitted calls
are drained and their observed usage remains counted. Consequently a failed
parallel document can cost more than a failed sequential document. A document
failure does not close a shared batch transport. Service shutdown rejects new
registration and public factory calls, drains already registered work using its
frozen transport contract, and only then closes owned workers. Concurrent close
calls share one cleanup owner; interrupted waiting retains resources for retry.

Each run stores a separate versioned `extraction_options` contract: concurrency,
pinned binary path, model, configured timeout, output limit, rates and the document
generation assigned transactionally by Store. It is separate from HTTP
`execution_options`. Invalid contracts fail before model calls. Registration
captures configuration, and execution does not recapture mutable CLI settings.
Checkpoints require this full contract to match, in addition to the existing
source, dictionary, model and prompt contract; changing concurrency restarts from
chunk one. Legacy checkpoints remain usable only by the default path. Successfully
published extractions still cannot be repeated merely by changing concurrency.

Experimental publication is one transaction after all chunks pass. It checks the current
document version, experiment generation and approved dictionary revision/hash.
The default path retains its existing historical-version publication behavior.
Checkpoint completion follows the successful transaction; optional recording
failure after commit cannot overwrite the committed run status. All new facts
remain pending human approval.

## Isolated benchmark

### Original-document chunk threshold candidate

`Service.start(..., extraction_policy='chunk_threshold_v1')` selects one worker
for one to three chunks and two workers for four or more chunks. The count uses
the entire stored source and production `chunks()` inside the registration
transaction. Cold runs use the same rule. There is no readiness condition.
The option is experimental, off by default, exclusive with explicit concurrency,
and unavailable through HTTP.

Schema 2 stores the policy, original chunk count, chosen concurrency, transport
snapshot and document generation. Execution validates the original source before
creating a caller; resuming a checkpoint never selects concurrency from the
remaining suffix. Schema 1 explicit-concurrency runs retain their contract.

The benchmark flag `--extraction-policy chunk_threshold_v1` uses the registered
decision to create its transport. In batch mode a capacity change closes the old
pool before creating the next; replacement time and a new owner ID are recorded.
All initial and repair calls retain the shared request/time/token budget. Cleanup
failure stops admission. This local implementation does not establish a speedup:
the new experiment follows `extraction-chunk-threshold-ab-contract.md`, and real
model calls require separate approval of a concrete budget.

### Explicit concurrency

After separately approving a real-call budget, use the existing benchmark CLI
with `--mode cli --agent claude --model <explicit-model>`,
`--process-mode document|batch`, and `--extraction-concurrency 1|2`.
The ordinary request/time/token limits and `--allow-model-calls` requirement still
apply. The scheduler receives the budget wrapper itself; all initial and repair
calls pass through the shared budget. Document mode owns a pool per repetition;
batch mode retains one pool and closes it at the end. Timing schema 2 reports
request work sums separately from overlapping wall time and real order waits.

The optional `Service(..., extraction_caller=...)` injection is an internal trusted
embedding seam. Its immutable `extraction_spec` must match the registered contract;
it receives every call, including repairs, and remains responsible for its own
transport lifetime. It is not request JSON or an HTTP option. Tests use this seam
with synthetic responses and also exercise the real transport using local fake
streaming subprocesses. These tests do not establish model performance. Adoption
requires the frozen A/B contract in `extraction-sonnet-baseline-completed.md`.

## Schema upgrade and rollback

Store adds nullable `runs.extraction_options TEXT` in its existing immediate
transaction. Existing rows remain NULL; there is no backfill or destructive change.
Repeated startup is idempotent. Legacy writers that name existing columns continue
to insert NULL, and legacy readers selecting their existing columns remain valid.

Before downgrading, stop admission and drain active jobs, or restart with normal
recovery to mark queued/running jobs interrupted. Do not hand a queued experiment
to old code that cannot enforce its contract. The additive column may remain on
downgrade. Old code cannot reuse experimental checkpoint contracts and must start
those extractions again. A pre-upgrade SQLite backup can restore the prior schema
and data, but discards all work written since the backup; never restore over a
running service. Tests exercise rollback of interrupted DDL, repeated upgrades,
old/new row compatibility and backup restore in disposable databases only.
