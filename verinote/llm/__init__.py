# SPDX-License-Identifier: MPL-2.0
"""Provider-agnostic LLM layer.

`LLMClient` is the single seam the rest of verinote talks to. Concrete adapters
(one per entry in `config.PROVIDERS`) normalise structured (JSON-schema) output
in-house so no vendor API leaks upward. This is the anti-lock-in design:
the deterministic DuckDB-backed verifier re-checks every fact, so the provider/model
is freely swappable.
"""

from verinote.llm.base import MIN_REDACTABLE_SECRET, ExtractedFact, LLMClient, LLMError
from verinote.llm.factory import get_client

__all__ = ["LLMClient", "ExtractedFact", "LLMError", "MIN_REDACTABLE_SECRET", "get_client"]
