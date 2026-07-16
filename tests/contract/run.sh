#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Convenience wrapper for the issue #241 provider contract tests:
#
#   VN_CONTRACT_PROVIDER=claudecli tests/contract/run.sh
#
# The "every contract test skipped" guard lives in tests/contract/conftest.py's
# pytest_sessionfinish, not here, so it holds for *any* run that selects the
# contract marker — including `python -m pytest -m contract` from the repo root.
# This script only fixes the working directory and the standard flags.
#
# Exit codes are pytest's own; a fully-skipped opt-in run exits non-zero via that
# session guard.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here"

python="${PYTHON:-python}"
exec "$python" -m pytest tests/contract -m contract -rs "$@"
