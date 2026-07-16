#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Run the issue #241 provider contract tests and fail loudly if they were all
# skipped. A fully-skipped opt-in run is the harness's worst failure mode: it
# looks green while exercising nothing. Set the gate to actually run the guards:
#
#   VERINOTE_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
#
# Exit codes:
#   0  contract tests ran (and pytest itself passed)
#   1  contract tests were collected but every one skipped (no guard executed)
#   *  pytest's own exit code when tests ran and some failed/errored
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here"

python="${PYTHON:-python}"
output="$("$python" -m pytest tests/contract -m contract -rs "$@" 2>&1)"
status=$?
printf '%s\n' "$output"

# Count the tests the `-m contract` filter actually *selected*, not every item
# collected (the module also holds non-contract meta tests that get deselected).
# pytest prints "N selected" only when something was deselected; fall back to the
# item count otherwise.
selected="$(printf '%s\n' "$output" | grep -oE '[0-9]+ selected' | head -1 | grep -oE '[0-9]+' || true)"
if [ -z "$selected" ]; then
  selected="$(printf '%s\n' "$output" | grep -oE '[0-9]+ item' | head -1 | grep -oE '[0-9]+' || true)"
fi
if printf '%s\n' "$output" | grep -qE '[0-9]+ (passed|failed|error|errors|xpassed|xfailed)'; then
  ran=1
else
  ran=0
fi

if [ "${selected:-0}" -gt 0 ] && [ "$ran" -eq 0 ]; then
  echo "run.sh: ${selected} contract test(s) selected but none executed (all skipped)." >&2
  echo "run.sh: set VERINOTE_CONTRACT_PROVIDER=claudecli|ollama|... to run them." >&2
  exit 1
fi

exit "$status"
