# SPDX-License-Identifier: MPL-2.0
"""Prompt library with packaged defaults and KB-local markdown overrides."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

PromptId = Literal[
    "extraction",
    "ollama-extraction",
    "query-translation",
    "query-intent",
    "focused-role-extraction",
    "ask-fallback",
    "extraction-limit-hint",
    "claude-json-wrapper",
]
PromptSource = Literal["default", "override"]


class PromptError(ValueError):
    """Raised when a prompt key or prompt text violates the prompt contract."""


class PromptUnavailableError(RuntimeError):
    """A required prompt could not be loaded at all — an availability condition.

    A prompt file the pipeline cannot read is a condition of the host (a file the
    machine cannot decode), not of the source's content. It must not be charged to
    a chunk's content retry budget the way an `LLMError` is: an availability
    condition stops the job once and leaves the budget intact, so the source is
    not given up on over a file the user can fix (#269, #544).

    A `RuntimeError` on purpose (like `PolicyMissingError`), not a `PromptError`
    (`ValueError`) subclass: the routing clauses that take it sit above the broad
    `except Exception`, and no `except ValueError` site should also claim it.
    """


@dataclass(frozen=True)
class PromptDefinition:
    id: PromptId
    title: str
    filename: str
    required_placeholders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prompt:
    definition: PromptDefinition
    text: str
    default_text: str
    source: PromptSource
    override_path: Path

    @property
    def id(self) -> PromptId:
        return self.definition.id

    @property
    def title(self) -> str:
        return self.definition.title


_DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition("extraction", "Extraction", "extraction.md"),
    PromptDefinition(
        "ollama-extraction",
        "Ollama extraction",
        "ollama-extraction.md",
        ("max_facts",),
    ),
    PromptDefinition(
        "query-translation",
        "Datalog translation",
        "query-translation.md",
        ("qid",),
    ),
    PromptDefinition("query-intent", "Query intent", "query-intent.md"),
    PromptDefinition(
        "focused-role-extraction",
        "Focused role extraction",
        "focused-role-extraction.md",
    ),
    PromptDefinition("ask-fallback", "Ask fallback answer", "ask-fallback.md"),
    PromptDefinition(
        "extraction-limit-hint",
        "Extraction limit hint",
        "extraction-limit-hint.md",
        ("max_facts",),
    ),
    PromptDefinition(
        "claude-json-wrapper",
        "Claude JSON wrapper",
        "claude-json-wrapper.md",
        ("schema_json",),
    ),
)
_BY_ID = {definition.id: definition for definition in _DEFINITIONS}


def list_prompts() -> tuple[PromptDefinition, ...]:
    return _DEFINITIONS


def prompt_definition(prompt_id: str) -> PromptDefinition:
    try:
        return _BY_ID[prompt_id]  # type: ignore[index]
    except KeyError as exc:
        raise PromptError(f"unknown prompt: {prompt_id}") from exc


def default_prompt_text(prompt_id: str) -> str:
    definition = prompt_definition(prompt_id)
    text = (
        resources.files("verinote.prompts")
        .joinpath("defaults", definition.filename)
        .read_text(encoding="utf-8")
    )
    return _normalize_prompt_text(text)


def prompt_override_path(root: Path, prompt_id: str) -> Path:
    definition = prompt_definition(prompt_id)
    return Path(root).expanduser().resolve() / "policy" / "prompts" / definition.filename


def get_prompt(root: Path, prompt_id: str) -> Prompt:
    definition = prompt_definition(prompt_id)
    default_text = default_prompt_text(definition.id)
    override_path = prompt_override_path(root, definition.id)
    source: PromptSource = "default"
    text = default_text
    if override_path.is_file():
        override_text = _normalize_prompt_text(override_path.read_text(encoding="utf-8"))
        if override_text:
            _validate_prompt_text(definition, override_text)
            text = override_text
            source = "override"
    _validate_prompt_text(definition, text)
    return Prompt(
        definition=definition,
        text=text,
        default_text=default_text,
        source=source,
        override_path=override_path,
    )


def readable_override_text(path: Path) -> str | None:
    """The stored override at `path`, read the way `get_prompt` reads it — or None.

    `None` means "absent, empty after normalization, or unreadable by this
    process", and NO exception leaves this function. It mirrors `get_prompt`'s
    read clause — the `is_file()` guard first, a `read_text` as UTF-8, then the
    SAME normalization — because its one caller
    (`verinote/web/app.py::_prompts_page`) discriminates between two
    `PromptError` states by whether this answer is non-empty: non-`None` means
    the stored text is what failed validation, so the page offers the editor
    seeded from it and a reset that deletes it; `None` means the packaged
    default is what failed, so the page offers neither — a reset there would
    delete the user's file and fix nothing (#546). The two answers must never
    diverge from what `get_prompt` actually did with this file, or the page
    offers a destructive control on a guess.

    `None` on empty-after-normalization is the discriminator, not a detail:
    `get_prompt` SKIPS an empty override (and then validates the default), so
    it must read as `None` here too — a non-`None` answer for an empty file
    would classify a packaged-default fault as a stored-override fault and
    hand out a Reset that deletes a file the loader never used.

    Broad except, the way `_override_is_unreadable` in the app does it: an
    unreadable parent directory makes `is_file()` itself raise
    (`PermissionError`, measured), and the caller renders the page this answer
    feeds — a raise here would take that page down instead of letting it
    choose.
    """
    try:
        if not path.is_file():
            return None
        return _normalize_prompt_text(path.read_text(encoding="utf-8")) or None
    except Exception:  # noqa: BLE001 - "absent, empty, or unreadable" IS the answer
        return None


def render_prompt(root: Path, prompt_id: str, **values: object) -> str:
    prompt = get_prompt(root, prompt_id)
    text = prompt.text
    for placeholder in prompt.definition.required_placeholders:
        if placeholder not in values:
            raise PromptError(f"missing prompt value: {placeholder}")
        text = text.replace("{" + placeholder + "}", str(values[placeholder]))
    return text


def save_prompt_override(root: Path, prompt_id: str, text: str) -> Path:
    definition = prompt_definition(prompt_id)
    normalized = _normalize_prompt_text(text)
    if not normalized:
        raise PromptError("prompt text is required")
    _validate_prompt_text(definition, normalized)
    path = prompt_override_path(root, definition.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n", encoding="utf-8")
    return path


def delete_prompt_override(root: Path, prompt_id: str) -> None:
    path = prompt_override_path(root, prompt_id)
    if path.exists():
        path.unlink()


def _normalize_prompt_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _validate_prompt_text(definition: PromptDefinition, text: str) -> None:
    if not text:
        raise PromptError("prompt text is required")
    for placeholder in definition.required_placeholders:
        token = "{" + placeholder + "}"
        if token not in text:
            raise PromptError(
                f"{definition.title} prompt must include required placeholder {token}"
            )
