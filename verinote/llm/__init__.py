# SPDX-License-Identifier: MPL-2.0
"""Provider-agnostic LLM layer.

`LLMClient` is the single seam the rest of verinote talks to. Concrete adapters
(Anthropic / Claude CLI / OpenAI / Ollama) normalise structured (JSON-schema)
output in-house so no vendor API leaks upward. This is the anti-lock-in design:
the deterministic wirelog verifier re-checks every fact, so the provider/model
is freely swappable.
"""

from verinote.llm.base import ExtractedFact, LLMClient, LLMError
from verinote.llm.factory import get_client

__all__ = ["LLMClient", "ExtractedFact", "LLMError", "get_client"]
