# SPDX-License-Identifier: Apache-2.0
"""Orchestration: extract -> (review) -> compile -> check.

The pipeline is intentionally thin glue over store/, llm/ and engine/. The human
review gate sits between extract and compile and is driven from the web UI.
"""

from verinote.pipeline.extract import extract_source
from verinote.pipeline.verify import verify

__all__ = ["extract_source", "verify"]
