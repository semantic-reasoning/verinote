# SPDX-License-Identifier: MPL-2.0
"""Claude Code CLI adapter. Uses `claude -p --json-schema` and parses stdout."""

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
    name = "ClaudeCLI"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        prompt = _prompt(
            system=EXTRACTION_SYSTEM + ("\n" + schema_hint if schema_hint else ""),
            schema=FACT_ARRAY_SCHEMA,
            user=source_text,
        )
        return parse_facts(self._run(prompt, schema=FACT_ARRAY_SCHEMA))

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        prompt = _prompt(
            system=query_system(qid) + ("\n" + schema_hint if schema_hint else ""),
            schema=QUERY_SCHEMA,
            user=question,
        )
        return parse_query(self._run(prompt, schema=QUERY_SCHEMA))

    def _run(self, prompt: "_Prompt", *, schema: dict[str, Any]) -> str:
        schema_json = json.dumps(schema, ensure_ascii=False)
        cmd = [
            "claude",
            "--system-prompt",
            prompt.system,
            "--json-schema",
            schema_json,
            "-p",
            prompt.user,
        ]
        if self.cfg.model:
            cmd = ["claude", "--model", self.cfg.model, *cmd[1:]]
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


class _Prompt:
    def __init__(self, *, system: str, user: str) -> None:
        self.system = system
        self.user = user


def _prompt(*, system: str, schema: dict[str, Any], user: str) -> _Prompt:
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return _Prompt(
        system=(
            f"{system}\n\n"
            "Return only a single JSON object. Do not wrap it in Markdown fences. "
            "The JSON object must match this schema:\n"
            f"{schema_json}"
        ),
        user=(
            "Input:\n"
            f"{user}"
        ),
    )
