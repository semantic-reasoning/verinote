# SPDX-License-Identifier: MPL-2.0
"""OpenRouter adapter — one endpoint that routes to many vendors' models.

OpenRouter speaks the OpenAI wire protocol, so every generation path is
inherited from `OpenAIAdapter` unchanged. What this module adds is the part
that is *not* the protocol: an endpoint bound to the provider the user picked
rather than to a text field they can leave blank.

That binding is why this is a provider of its own instead of a documented
`openai` + `base_url` recipe. Under the recipe an unset `base_url` resolves to
`api.openai.com`, so a user who chose OpenRouter and cleared the field would
ship their documents to a vendor they never selected — the confidentiality
failure `assert_settings_intact` exists to prevent (#269), arriving through
ordinary configuration instead of corruption. `OllamaAdapter` binds its
endpoint the same way and for the same reason.
"""

from __future__ import annotations

from verinote.llm.openai_adapter import OpenAIAdapter

# The endpoint an unset `base_url` resolves to. Named rather than inlined so a
# settings surface can eventually report the same URL this adapter dials; today
# the only consumers are `_base_url` and its tests.
OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(OpenAIAdapter):
    """OpenAI-protocol generation against OpenRouter.

    `name` is not cosmetic: the inherited request paths raise
    `LLMError(f"{self.name} request failed: ...")`, and that string is what the
    settings banner shows and what a failed extraction persists as its chunk
    error. Left at `openai` it would name the wrong provider in the one place a
    user looks to find out which provider broke.
    """

    name = "openrouter"

    def _base_url(self) -> str:
        return self.cfg.base_url or OPENROUTER_DEFAULT_BASE_URL
