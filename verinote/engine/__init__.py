# SPDX-License-Identifier: Apache-2.0
"""wirelog engine integration: compile confirmed facts to `.dl`, run the check."""

from verinote.engine.wirelog import CheckReport, compile_dl, run_check

__all__ = ["compile_dl", "run_check", "CheckReport"]
