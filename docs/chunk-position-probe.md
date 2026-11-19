# Chunk position probe (#811 gate A)

Question: is a fact-bearing passage extracted less often when it sits at the back of an 8,000-character chunk
than at the front or middle? Only a back-position loss would justify testing smaller chunks (gate B).

## Setup (pre-registered in `verinote/diagnostics/chunk_position_probe.py`)

- Model `gpt-6-luna`, production prompt path (extraction prompt, chunk payload, pinned approved dictionary).
- One passage of four directly stated relations, placed at the front, middle and back of an otherwise identical
  chunk (7,973 characters, prompt 80,981 bytes, ~58 KB synthetic dictionary).
- Three interleaved, rotated repetitions per position (9 calls planned). A triple counts only with a quote inside
  its own sentence that names both entities and the relation verb. A response the product's own validation
  rejects makes the run `inconclusive`, and the run stops there.
- `adopt_late_loss` needs the back mean lowest and a gap of at least one triple that exceeds the within-position spread.

## Result (`benchmarks/extraction/results/811-chunk-position/`)

| Call | Position | Triples recovered | Product-valid |
|---|---|---|---|
| 1 | front | 4 / 4 | yes |
| 2 | middle | 4 / 4 | yes |
| 3 | back | 4 / 4 | yes |
| 4 | middle (rep 2) | 4 / 4 stated, not scored | no: `claims` was empty |

4 calls, 1.78 credits, 67 seconds. Verdict by the pre-registered rule: **inconclusive**, stopped after call 4
because the product requires 1-12 claims and call 4 returned none. The cause is unknown: calls 1-3 produced
claims from the same passage. The four facts in call 4 were complete and correctly quoted. In `analysis.json`
the `0` in `scores.middle` is that unscored call, not a measured loss.

## Decision

- No evidence of a back-position loss: every product-valid call, including the back position, recovered all four
  triples, and the one rejected call also stated all four.
- The probe could not reach its own bar for an effect, so this is not a measured rejection either. The
  issue's stop rule applies: keep the 8,000-character chunk size and do not run gate B (117 calls per document,
  50-130 credits). Reopen only with new evidence of late-part loss in real documents.
- The 8,000-character checkpoint contract is unchanged. `test_checkpoint_from_another_chunk_size_is_not_reused_and_every_span_is_sent`
  pins that the recorded `chunk_size` equals the spacing of the spans actually sent, and that a checkpoint
  recorded with a different `chunk_size` is not reused and every span is sent. It does not exercise a chunker
  change: 8,000 is hard-coded in several places (loss-audit offsets, coverage), so changing it is gate B work.
- Measurement code is in `verinote/diagnostics/` and is not imported by any default-path call.
