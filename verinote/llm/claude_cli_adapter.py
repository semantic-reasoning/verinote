# SPDX-License-Identifier: MPL-2.0
"""Claude Code CLI adapter. Uses `claude -p --json-schema` and parses stdout."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError
from verinote.llm.schema import (
    FACT_ARRAY_SCHEMA,
    QUERY_INTENT_SCHEMA,
    QUERY_SCHEMA,
    parse_facts,
    parse_query,
)
from verinote.pipeline.query_intent import QueryIntent, parse_query_intent
from verinote.prompts import PromptError, render_prompt

_MODEL_ALIASES = {
    "fable": "fable",
    "opus": "opus",
    "sonnet": "sonnet",
}


class ClaudeCliAdapter:
    name = "ClaudeCLI"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        prompt = _prompt(
            system=_with_schema_hint(
                _render_prompt(self.cfg.root, "extraction"), schema_hint
            ),
            schema=FACT_ARRAY_SCHEMA,
            user=source_text,
            root=self.cfg.root,
        )
        return parse_facts(self._run(prompt, schema=FACT_ARRAY_SCHEMA))

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        prompt = _prompt(
            system=_with_schema_hint(
                _render_prompt(self.cfg.root, "query-translation", qid=qid),
                schema_hint,
            ),
            schema=QUERY_SCHEMA,
            user=question,
            root=self.cfg.root,
        )
        return parse_query(self._run(prompt, schema=QUERY_SCHEMA))

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> QueryIntent:
        prompt = _prompt(
            system=_with_schema_hint(
                _render_prompt(self.cfg.root, "query-intent"), schema_hint
            ),
            schema=QUERY_INTENT_SCHEMA,
            user=question,
            root=self.cfg.root,
        )
        return parse_query_intent(self._run(prompt, schema=QUERY_INTENT_SCHEMA))

    def answer_question(self, *, question: str, context: str) -> str:
        prompt = _Prompt(
            system=_render_prompt(self.cfg.root, "ask-fallback"),
            user=f"Question:\n{question}\n\nContext:\n{context}",
        )
        return self._run_text(prompt)

    def _run(self, prompt: "_Prompt", *, schema: dict[str, Any]) -> str:
        schema_json = json.dumps(schema, ensure_ascii=False)
        cmd = [
            "claude",
            "--safe-mode",
            "--no-session-persistence",
            "--system-prompt",
            prompt.system,
            "--json-schema",
            schema_json,
            "-p",
            prompt.user,
        ]
        model = _cli_model(self.cfg.model)
        if model:
            cmd = ["claude", "--model", model, *cmd[1:]]
        try:
            with tempfile.TemporaryDirectory(prefix="verinote-claudecli-") as tmpdir:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    cwd=tmpdir,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    timeout=self.cfg.llm_timeout_seconds,
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

    def _run_text(self, prompt: "_Prompt") -> str:
        cmd = [
            "claude",
            "--safe-mode",
            "--no-session-persistence",
            "--system-prompt",
            prompt.system,
            "-p",
            prompt.user,
        ]
        model = _cli_model(self.cfg.model)
        if model:
            cmd = ["claude", "--model", model, *cmd[1:]]
        try:
            with tempfile.TemporaryDirectory(prefix="verinote-claudecli-") as tmpdir:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    cwd=tmpdir,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    timeout=self.cfg.llm_timeout_seconds,
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


def _prompt(*, system: str, schema: dict[str, Any], user: str, root: Path) -> _Prompt:
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return _Prompt(
        system=(
            f"{system}\n\n"
            f"{_render_prompt(root, 'claude-json-wrapper', schema_json=schema_json)}"
        ),
        user=(
            "Input:\n"
            f"{user}"
        ),
    )


def _with_schema_hint(prompt: str, schema_hint: str) -> str:
    return prompt + ("\n" + schema_hint if schema_hint else "")


def _render_prompt(root, prompt_id: str, **values: object) -> str:
    try:
        return render_prompt(root, prompt_id, **values)
    except PromptError as exc:
        raise LLMError(str(exc)) from exc


def _cli_model(model: str) -> str:
    """Convert UI/display model names to Claude CLI aliases."""
    normalized = re.sub(r"[^a-z0-9]+", "", model.casefold())
    for key, value in _MODEL_ALIASES.items():
        if key in normalized:
            return value
    return model.strip()
