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
# The aliases the CLI's own `--model` help documents, in its order. Exported so
# the settings UI offers exactly what `_cli_model` recognises: a picker that
# listed an alias this adapter cannot resolve would be advertising a choice that
# silently does something else.
CLI_MODEL_ALIASES = tuple(_MODEL_ALIASES)

# A canonical model id -- lowercase, hyphen-separated, at least three segments
# (`claude-opus-4-8`, `claude-haiku-4-5`, `claude-3-opus-20240229`). The CLI
# accepts these verbatim, so they must reach it unchanged; only looser *display*
# names ("Opus 4.8", "Claude Opus") are collapsed to an alias.
_CANONICAL_MODEL_ID = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)+$")

# The CLI takes the prompt as an exec ARGUMENT, and an argument cannot carry
# every str Python can hold. Two kinds fail, both before the process is spawned:
# a NUL byte, and a surrogate outside the U+DC80..U+DCFF range that `os.fsencode`
# round-trips (D800..DC7F, DD00..DFFF -- what `json.loads('"\\ud800"')` yields).
_UNSENDABLE_ARGUMENT = (
    "claude CLI request failed: the text could not be sent. It contains a "
    "character that cannot travel in a command-line argument -- a NUL byte, or "
    "an unpaired surrogate left behind by a bad decode. Remove it from the "
    "source file and re-ingest that source."
)

# `text=True` makes subprocess decode BOTH of the CLI's streams in the
# interpreter's locale encoding -- UTF-8 wherever this runs today, so a bad byte
# on stdout or on stderr can land here. (Under a latin-1 locale nothing fails to
# decode at all and this clause goes unreachable; pinning `encoding=` so that
# stops being true is left to the follow-up that owns it.) What this clause
# learns is that those bytes did not decode -- not which stream carried them,
# and not that the answer itself was lost: a valid JSON reply on stdout is
# discarded all the same when one byte of the log beside it will not decode. The
# source text is one place those bytes can come from -- the U+DC80..U+DCFF band
# above reaches the CLI as raw 0x80-0xFF and can echo back.
#
# So the message names the operation that failed, then states the one thing this
# code did establish -- the argv was accepted, i.e. the text got as far as the
# CLI -- and points at the version. It deliberately does NOT say the send
# succeeded: the CLI may have failed to relay the text onward and be reporting
# exactly that in the bytes that will not decode.
_UNDECODABLE_OUTPUT = (
    "claude CLI request failed: the CLI's output was not valid UTF-8 and could "
    "not be read. The text reached the CLI; it is the CLI's output that could "
    "not be read. Check the claude CLI's version if it recurs."
)


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
        return self._invoke(cmd)

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
        return self._invoke(cmd)

    def _invoke(self, cmd: list[str]) -> str:
        """Run `cmd` and return its stdout, or raise `LLMError`.

        One body, because the two call sites' copy-pasted `except` clauses missing
        the same case IS this bug (#474). The `with` is OUTSIDE the clause block
        below so that `try` guards exactly one statement: in this repo `ValueError`
        is also a domain type (`CorroborationPolicyError` subclasses it), and the
        only reason a broader `except ValueError` is safe here is that nothing else
        runs inside. Structure, not a comment.

        Which is why the temp directory is not simply left outside the contract.
        Creating it (ENOSPC, an unwritable TMPDIR) and cleaning it up can each
        fail, and §10.1 says every failure of an LLM call reaches the caller as
        `LLMError` -- landing on the web worker's generic `except Exception` as
        "analysis failed: [Errno 28] ..." is the exact shape #474 was reported as.
        So creation gets its own clause and one `except OSError` out here covers the
        rest. That one may be broad where the `ValueError` above may not, for the
        reason above: `OSError` is not a domain type in this repo.
        """
        try:
            tmp = tempfile.TemporaryDirectory(prefix="verinote-claudecli-")
        except OSError as exc:
            raise LLMError(f"claude CLI request failed: {exc}") from exc
        try:
            with tmp as tmpdir:
                try:
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
                except UnicodeDecodeError as exc:
                    # The CLI's OUTPUT could not be decoded. Do not tell the user to
                    # fix their source. Constant message: this exception's text
                    # carries byte values and offsets from the model's output, which
                    # derives from the user's document.
                    raise LLMError(_UNDECODABLE_OUTPUT) from exc
                except ValueError as exc:
                    # The ARGUMENT could not be encoded. MUST stay below
                    # UnicodeDecodeError, which is a ValueError subclass. The
                    # character this names is by definition an unsendable surrogate
                    # or a NUL, so it is not document content; only a position
                    # offset leaks, and an offset alone is not an oracle. That is
                    # what makes qualifying the message with the cause safe here and
                    # not in the clause above.
                    raise LLMError(f"{_UNSENDABLE_ARGUMENT} ({type(exc).__name__}: {exc})") from exc
        except OSError as exc:
            # Every OSError from the whole region: the spawn's own (a permission
            # error, a broken pipe -- `FileNotFoundError` is claimed above and does
            # not reach here) and the directory's cleanup. ONE clause, out here,
            # because base caught both with one clause and duplicating it inside
            # buys nothing -- an inner copy is unreachable-by-equivalence, which is
            # how a handler rots without a single test noticing.
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
    """Convert UI/display model names to Claude CLI aliases.

    A *canonical* model id is passed through untouched. The substring match
    below is deliberately loose so "Opus 4.8" resolves to `opus`, but that same
    looseness silently rewrote `claude-opus-4-8` -- a pinned id the CLI accepts
    verbatim -- into `opus`, i.e. whatever is newest. The KB's config.json then
    named one model while the CLI ran another, and the pin was lost with no
    error. Canonical ids are recognised first so a pin stays a pin; an id that
    turns out not to exist is the CLI's own loud `exit 1`, not a silent
    downgrade to a model the user did not choose.
    """
    stripped = model.strip()
    # Canonical ids are lowercase by definition, but a typed-in `CLAUDE-OPUS-4-8`
    # is still unambiguously that pin -- fold the case rather than sending it
    # down the alias path, which would drop the pin over capitalisation alone.
    folded = stripped.casefold()
    if _CANONICAL_MODEL_ID.match(folded):
        return folded
    normalized = re.sub(r"[^a-z0-9]+", "", model.casefold())
    for key, value in _MODEL_ALIASES.items():
        if key in normalized:
            return value
    return stripped
