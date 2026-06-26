# SPDX-License-Identifier: MPL-2.0
"""Select an `LLMClient` adapter from config — the one place provider choice lives."""

from __future__ import annotations

from verinote.config import Config
from verinote.llm.base import LLMClient, LLMError


def get_client(cfg: Config) -> LLMClient:
    provider = cfg.provider
    if provider == "anthropic":
        from verinote.llm.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(cfg)
    if provider == "claude":
        from verinote.llm.claude_cli_adapter import ClaudeCliAdapter

        return ClaudeCliAdapter(cfg)
    if provider == "openai":
        from verinote.llm.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(cfg)
    if provider == "ollama":
        from verinote.llm.ollama_adapter import OllamaAdapter

        return OllamaAdapter(cfg)
    raise LLMError(
        f"unknown VERINOTE_PROVIDER={provider!r}; expected anthropic|claude|openai|ollama"
    )
