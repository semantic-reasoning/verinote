# SPDX-License-Identifier: MPL-2.0
"""Claude Code CLI adapter. Uses `claude -p` and parses JSON from stdout."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import (
    EXTRACTION_SYSTEM,
    FACT_ARRAY_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
    query_system,
)


class ClaudeCliAdapter:
    name = "claude"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        prompt = _json_prompt(
            system=EXTRACTION_SYSTEM + ("\n" + schema_hint if schema_hint else ""),
            schema=FACT_ARRAY_SCHEMA,
            user=source_text,
        )
        return parse_facts(self._run(prompt))

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        prompt = _json_prompt(
            system=query_system(qid) + ("\n" + schema_hint if schema_hint else ""),
            schema=QUERY_SCHEMA,
            user=question,
        )
        return parse_query(self._run(prompt))

    def _run(self, prompt: str) -> str:
        cmd = ["claude", "-p", prompt]
        if self.cfg.model:
            cmd = ["claude", "--model", self.cfg.model, "-p", prompt]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as exc:
            raise LLMError("claude CLI not found; install Claude Code and ensure `claude` is on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError("claude CLI request timed out") from exc
        except OSError as exc:
            raise LLMError(f"claude CLI request failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise LLMError(f"claude CLI exited with {proc.returncode}: {detail}")
        return proc.stdout.strip()


def _json_prompt(*, system: str, schema: dict[str, Any], user: str) -> str:
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        f"{system}\n\n"
        "Return only a single JSON object. Do not wrap it in Markdown fences. "
        "The JSON object must match this schema:\n"
        f"{schema_json}\n\n"
        "Input:\n"
        f"{user}"
    )
