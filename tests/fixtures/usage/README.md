These fixtures preserve telemetry shapes observed from five installed CLIs on
2026-10-15 using only: `Do not use any tools. Return exactly {"ok":true} and nothing else.`
They contain no user document content. Session IDs, paths and incidental events
were removed; answer bodies are the synthetic object. Claude modelUsage retains
only its model key because aggregate usage and total_cost_usd are authoritative
for this adapter. Cursor keeps system init and final result. Codex keeps the
final agent_message and turn.completed. Copilot is its dedicated usage file,
separate from plain stdout. Numeric token and cost values were retained to test
cache partitions and credit conversion. They are protocol examples, not default
prices or estimates of document-processing cost.

`openrouter.json` is a synthetic, non-streaming API envelope for #706, not an
observed paid request. Its generated content deliberately contains fake usage;
only the outer API usage is evidence. Cache and reasoning counts are subsets of
prompt/completion totals. `cost` is reported OpenRouter credits; upstream cost
is not added to billing or converted to reported USD. Missing/zero, malformed
numbers and subset contradictions are derived in `test_http_records.py`.
