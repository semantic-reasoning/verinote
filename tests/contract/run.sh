#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Convenience wrapper for the issue #241 provider contract tests:
#
#   VN_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
#
# The "every contract test skipped" guard lives in tests/contract/conftest.py's
# pytest_sessionfinish, not here, so it holds for any run that asks for these
# guards — `python -m pytest -m contract` or `python -m pytest tests/contract`
# from the repo root alike. This script only fixes the working directory and the
# standard flags.
#
# Exit codes are pytest's own; a fully-skipped opt-in run exits non-zero via that
# session guard.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here"

# Discover an interpreter rather than hard-coding one: `python3` is the only
# spelling on many systems (this repo's own dev machines included), while older
# images and activated virtualenvs expose `python` alone. Hard-coding either name
# breaks the README's headline command on half the platforms it documents.
# PYTHON wins when set, so a caller can always name the interpreter outright.
if [ -n "${PYTHON:-}" ]; then
  python="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  python="python3"
elif command -v python >/dev/null 2>&1; then
  python="python"
else
  echo "run.sh: no python3 or python found on PATH; set PYTHON=/path/to/python" >&2
  exit 1
fi

exec "$python" -m pytest tests/contract -m contract -rs "$@"
