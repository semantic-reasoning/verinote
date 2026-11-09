# Bounded extraction experiment

Chunks within each default extraction run remain sequential. `Service.start(...,
extraction_concurrency=1|2)` opts a Claude extraction run into the same bounded
scheduler with one or two independent workers. Copilot has a separate explicit
concurrency-2 opt-in described below. Document scheduling is independent: up to
two dictionary-generation files and two extraction files can run at once, with
one separate serialized question lane. The per-call contracts of other providers
are unchanged. The HTTP API does not
expose this experimental option.

The coordinator alone validates responses in source order, performs at most one
quotation repair per chunk (benchmarks also stop at two per document), and saves a
contiguous validated checkpoint prefix. Running calls plus completed responses awaiting acceptance
never exceed the chosen concurrency. The deadline begins at initial dispatch and
covers worker admission, request, ordering, repair and validation. A worker error
closes admission for both new initial calls and repairs; previously admitted calls
are drained and their observed usage remains counted. A chunk before the failing one whose admitted call (initial or repair) answers is still validated in source order and saved to the checkpoint when it passes without a new call after the failure; validation stops at the first chunk that fails validation, would need a repair call not yet admitted, or reaches its deadline. Consequently a failed
parallel document can cost more than a failed sequential document. A document
failure does not close a shared batch transport. Service shutdown rejects new
registration and public factory calls, drains already registered work using its
frozen transport contract. Each run closes its own workers after its admitted
calls drain; shutdown also retries remaining owner cleanup. Concurrent close
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
While such a run is queued or running, `Store.register` refuses a new source version of its document in the same write transaction, so the version and generation checks fail only after an out-of-store change.
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
and unavailable as an HTTP request option.

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

### Local web opt-in

Start the local server with `python -m verinote --data-dir data --port 8765
--claude-extraction-policy chunk_threshold_v1` to apply this policy to Claude web
extraction. Other providers, dictionary generation and questions are unchanged.
The startup log and GET `/api/settings` expose the policy and its startup source.
Clients cannot set or override it through request JSON.

Remove the flag and restart to restore the default for subsequent registrations.
Already registered runs retain their frozen contracts. Opt-in extraction uses the
experimental publication checks for the current document version and generation.
While such a run is queued or running, `Store.register` refuses a new source version of its document in the same write transaction, so the version and generation checks fail only after an out-of-store change.
Local activation does not establish measured performance or general adoption;
the separate frozen experiment and real-call approval requirements still apply.

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
recovery to mark queued/running jobs interrupted. Code that predates run cancel leaves a
`cancelling` row as it is, so restart the new code once first: its recovery ends that row
as cancelled. Do not hand a queued experiment
to old code that cannot enforce its contract. The additive column may remain on
downgrade. Old code cannot reuse experimental checkpoint contracts and must start
those extractions again. A pre-upgrade SQLite backup can restore the prior schema
and data, but discards all work written since the backup; never restore over a
running service. Tests exercise rollback of interrupted DDL, repeated upgrades,
old/new row compatibility and backup restore in disposable databases only.


## Copilot explicit concurrency 2

`Service.start(document_id, 'copilot', model, extraction_concurrency=2)` enables
at most two admitted initial chunks for that document. `None` preserves the
existing sequential Copilot argv, call count and ordering. Capacity 1, a threshold
policy, and dictionary generation are not accepted for this opt-in. Other
providers retain their transport contracts. Document scheduling independently permits two dictionary-generation files and two extraction files, plus one serialized question. Each registered extraction owns its transport; two files with chunk concurrency two may make four extraction calls at once.

For local web extraction, start with:

```sh
python -m verinote --data-dir data --port 8765 --copilot-extraction-concurrency 2
```

The default is disabled (sequential 1). The separate startup log and read-only
`GET /api/settings` field `copilot_extraction` report enabled state, effective
concurrency, `agent=copilot`, `stage=extract` and `source=server_startup`. Only
Copilot `/extract` registrations inherit it. HTTP request bodies and settings PUT
cannot enable or override it. The existing Claude flag and `extraction_policy`
settings object retain their meaning. Remove this flag and restart to return
future registrations to sequential operation; already registered runs retain
their frozen contract.

Schema 3 records the Copilot agent, absolute executable path, model, capacity 2,
timeout policy, output limit, copied Copilot rates, `no_auto_update=true` and
transactionally assigned document generation. The model may be empty or `auto`,
as in ordinary Copilot calls; the chosen route remains unknown until reported.
Every initial and quote-repair call uses the captured path and
`--no-auto-update`, even if PATH or the original CLI configuration later changes.
Each call starts a separate process with its own work/output/usage directory.
There is no Claude persistent worker, `/clear` or Copilot HOME isolation.
A pinned path is not a pinned binary version: the file at that path can change,
and `--no-auto-update` may select a different installed version from the default
sequential invocation. No version subprocess runs during registration.

The existing bounded scheduler validates and saves only a contiguous prefix,
then publishes once after all chunks pass. Later completed outputs waiting for
an earlier chunk remain in memory, so a restart, or a failure at that earlier
chunk, can require paying for them again. After a recorded failure no new initial or repair call is admitted;
already admitted calls are awaited within their deadlines and their observed
usage remains included. A cancel behaves the same way: after it commits no new initial or
repair call is admitted, calls already admitted are awaited within their deadlines, and
their responses are validated and saved in source order up to the first chunk that would
need a repair call not yet admitted, fails validation, or reaches its deadline; that chunk and the later ones
are called again by a rerun. Claude with a concurrency or threshold policy and Copilot
concurrency 2 share this scheduler and therefore these semantics. A generic nonzero exit is reported as such, without
claiming it was a rate limit. There is no automatic retry or backoff.

Copilot reports GitHub AI credits. Aggregation uses `reported_credits` grouped by
`credit_unit`; `reported_usd` remains null. Optional configured USD estimates are
separate. Missing provider usage or unreadable/missing usage records are
incomplete, never known zero. Failed or unaccepted calls may still incur costs.

These items remain **unverified**: actual Copilot concurrent-process behavior,
shared `$HOME/.copilot`/`COPILOT_HOME` state and credential-refresh collisions,
provider concurrency/rate limits, simultaneous billing behavior, `auto` routing,
and comparative latency/credits. Two processes can cost more after failure.
Local stub tests demonstrate orchestration, immutable configuration and usage
accounting only; they do not establish real-provider performance. Real-model
measurement requires separate approval specifying model, call count, duration
and credits budget. Existing Claude USD/modelUsage A/B judgments cannot assess
Copilot credits and are not reused as evidence.

Schema 1 and 2 remain byte-compatible Claude contracts and retain existing
checkpoint reuse. Schema 3 uses the same nullable JSON column; no migration or
backfill is performed and existing records are not rewritten. Its entire
contract participates in checkpoint identity: a different pinned binary or
other contract change restarts from chunk one. Old code without schema-3 support
cannot read schema-3 runs; merely removing the startup flag does not make those
records readable by old code. For a code downgrade, stop/drain the service and
restore a pre-feature SQLite backup in an isolated/stopped environment, accepting
loss of subsequent work, or retain a reader supporting schema 3. Disabling the
flag while keeping this version is the non-destructive configuration rollback.
