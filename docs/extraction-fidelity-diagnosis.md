# Offline extraction fidelity diagnosis

Run from the repository with its Python environment:

```sh
PYTHONPATH=. python scripts/diagnose_extraction_fidelity.py
```

The command prints JSON only and performs no model calls, subprocesses, HTTP
requests, database writes, repairs of retained artifacts, or publication. An
optional `--artifact PATH` must contain the same pinned history. Redirect output
to a separate diagnostic file, never into the retained artifact directory.

The supported Claude history is `768-threshold-v2-sonnet`, report SHA-256
`72a63ca70e3cfdc9bd9a771999a65ed537bb584d5c8aacfe054c2e4ece981a5f`.
Before interpreting it, the diagnostic compares the complete retained file index
and calls the existing offline `evaluate_saved`. A preserved rejected
prefix with any unverified later artifacts is insufficient here. Verification
failure exits with status 2 and is distinct from a production or fidelity failure.

The three Claude terminal results are parsed at their actual one-based chunk
indices. Exact prompts bind source offsets, approved dictionary, filename and
version to the production prompt. Call IDs and checkpoint outputs are linked.
Replay follows production shape/quote preflight, mechanical accounting repair
on a copy, page/fact validation and strict checkpoint validation. It records
mechanical changes and never generates a quote repair. The pinned history has
no mechanical or quote repair calls; unexpected repairs fail closed. All three
normalized outputs are evaluated together by the unchanged finite evaluator.

Each obligation row links its source evidence and eligible actual quoted claim
indices to identified baseline prompt clauses and frozen evaluator errors.
Coverage rows show expected status, actual status/indices, and missing evidence;
gap rows show actual references and wrongly cited or missing evidence. A single
quoted claim can legitimately satisfy several obligations, or claims can be
split/reordered: there is no required oracle ordering or claim count. Prose in
`text`, `reason`, or `detail` does not substitute for source-supported quotes.
The matrix explains finite annotated evidence checks, not general entailment.

Schema version 1 and `extraction-fidelity-v1` identify this diagnostic. Its own
module/package/script/document hashes are reported separately: historical
source authentication does **not** authenticate newly added diagnostic code.
The original artifact hashes are checked again after replay. No fixture, gold,
production prompt, default, or saved evaluator verdict is changed.

## Reproduced result

The pinned run passes production validation for all three chunks and has three
correctly grounded facts (finite fact precision, recall and citation accuracy
are 1.0), but fails the unchanged annotation evaluator with 16 errors:

- Negated-source evidence is missing from quoted claims in chunks 1 and 2;
  missing-relation source evidence is missing in all three chunks.
- `calls` coverage fails in all chunks; `archives` coverage fails in chunks 1 and 2.
- Every chunk has unexpected `missing_relation` gap evidence and incomplete gaps.

These are the evaluator's finite obligations, not a broader claim that every
sentence or natural-language inference has been checked. The report preserves
the actual quotes, references, expected statuses and identified prompt clauses
needed to inspect each failure. No candidate prompt is evaluated in this unit.
