# SPDX-License-Identifier: Apache-2.0
"""Runtime configuration: where the KB lives and which LLM provider to use.

Resolved from environment variables (and CLI overrides) so nothing about the
active provider is hard-coded — this is the anti-lock-in seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _root() -> Path:
    return Path(os.environ.get("VERINOTE_ROOT", "./data")).expanduser().resolve()


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    provider: str  # "anthropic" | "openai" | "ollama"
    model: str
    api_key: str | None
    base_url: str | None

    @classmethod
    def load(cls) -> "Config":
        root = _root()
        provider = os.environ.get("VERINOTE_PROVIDER", "anthropic").lower()
        defaults = {
            "anthropic": "claude-opus-4-8",
            "openai": "gpt-4o",
            "ollama": "llama3.1",
        }
        return cls(
            root=root,
            db_path=root / "kb.sqlite",
            provider=provider,
            model=os.environ.get("VERINOTE_MODEL", defaults.get(provider, "")),
            api_key=os.environ.get("VERINOTE_API_KEY"),
            base_url=os.environ.get("VERINOTE_BASE_URL"),
        )
