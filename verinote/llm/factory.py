# SPDX-License-Identifier: MPL-2.0
"""Select an `LLMClient` adapter from config — the one place provider choice lives."""

from __future__ import annotations

from verinote.config import (
    PROVIDERS,
    Config,
    assert_credentials_intact,
    assert_settings_intact,
)
from verinote.llm.base import LLMClient, LLMError


def get_client(cfg: Config) -> LLMClient:
    # First act, before any provider branch: a corrupt config.json must never
    # resolve to a silent cloud fallback. Every current and future caller is
    # protected here by construction, so no route enumeration is needed (#269).
    assert_settings_intact(cfg)
    # Same reasoning, different file: an unreadable credentials file must not
    # resolve to "no key" and let a provider be called unauthenticated.
    assert_credentials_intact(cfg)
    provider = cfg.provider
    if provider == "anthropic":
        from verinote.llm.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(cfg)
    if provider == "claudecli":
        from verinote.llm.claude_cli_adapter import ClaudeCliAdapter

        return ClaudeCliAdapter(cfg)
    if provider == "openai":
        from verinote.llm.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(cfg)
    if provider == "openrouter":
        from verinote.llm.openrouter_adapter import OpenRouterAdapter

        return OpenRouterAdapter(cfg)
    if provider == "ollama":
        from verinote.llm.ollama_adapter import OllamaAdapter

        return OllamaAdapter(cfg)
    # Derived from PROVIDERS rather than spelled out: a hand-written list here
    # goes stale the moment a provider is added, and this message is what tells
    # a user with a typo in config.json what they were allowed to write.
    raise LLMError(
        f"unknown VERINOTE_PROVIDER={provider!r}; expected {'|'.join(PROVIDERS)}"
    )
