# SPDX-License-Identifier: MPL-2.0
import subprocess
from types import SimpleNamespace

import pytest

from verinote.config import Config
from verinote.llm.base import LLMError
from verinote.llm.claude_cli_adapter import ClaudeCliAdapter
from verinote.llm.factory import get_client
from verinote.prompts import save_prompt_override


def _cfg(tmp_path, *, model: str = "", llm_timeout_seconds: float = 600.0) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claudecli",
        model=model,
        api_key=None,
        base_url=None,
        llm_timeout_seconds=llm_timeout_seconds,
    )


def test_factory_selects_claude_cli_adapter(tmp_path):
    assert isinstance(get_client(_cfg(tmp_path)), ClaudeCliAdapter)


def test_claude_cli_extracts_facts_from_stdout(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"facts":[{"subject":"Ada","relation":"is_a",'
                '"object":"mathematician","confidence":0.9}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    facts = ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada is a mathematician.")

    assert facts[0].subject == "Ada"
    assert calls[0][0][0] == "claude"
    assert "--safe-mode" in calls[0][0]
    assert "--no-session-persistence" in calls[0][0]
    assert "--json-schema" in calls[0][0]
    assert "--system-prompt" in calls[0][0]
    assert "-p" in calls[0][0]
    assert calls[0][1]["capture_output"] is True
    assert "cwd" in calls[0][1]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL


def test_claude_cli_uses_model_when_configured(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"datalog":"answer_q7(V) :- relation(V, \\"is_a\\", \\"person\\")."}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    line = ClaudeCliAdapter(_cfg(tmp_path, model="sonnet")).translate_query(question="Who?", qid=7)

    assert commands[0][0:3] == ["claude", "--model", "sonnet"]
    assert "--safe-mode" in commands[0]
    assert line.startswith("answer_q7")


def test_claude_cli_normalizes_display_model_names(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"facts":[]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ClaudeCliAdapter(_cfg(tmp_path, model="Opus 4.8")).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", "opus"]


def test_claude_cli_missing_binary_is_llm_error(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="claude CLI not found"):
        ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")


def test_claude_cli_nonzero_is_llm_error(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="not logged in"):
        ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")


def test_claude_cli_uses_kb_prompt_overrides(tmp_path, monkeypatch):
    commands = []
    save_prompt_override(tmp_path, "extraction", "Custom extraction instructions.")
    save_prompt_override(tmp_path, "claude-json-wrapper", "Schema contract:\n{schema_json}")

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")

    system = commands[0][commands[0].index("--system-prompt") + 1]
    assert system.startswith("Custom extraction instructions.")
    assert "Schema contract:" in system
    assert '"facts"' in system


# extract_facts drives the schema-constrained `_run` site; answer_question
# drives the separate `_run_text` site. Both hardcoded timeout=180 before, so
# each subprocess call site needs its own guard. 1234.0 differs from both the
# 600.0 default and the old 180 hardcode.
@pytest.mark.parametrize(
    "invoke",
    [
        lambda a: a.extract_facts(source_text="x"),
        lambda a: a.answer_question(question="q", context="c"),
    ],
)
def test_claude_cli_applies_configured_timeout(tmp_path, monkeypatch, invoke):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=1234.0)))

    assert calls[0]["timeout"] == 1234.0


def test_claude_cli_prompt_validation_error_is_llm_error(tmp_path):
    path = tmp_path / "policy" / "prompts" / "claude-json-wrapper.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing required placeholder.\n", encoding="utf-8")

    with pytest.raises(LLMError, match="\\{schema_json\\}"):
        ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")


# --- a pinned model id must survive to the CLI, not collapse to "latest" ---


def _record_commands(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-8",
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-3-opus-20240229",
    ],
)
def test_claude_cli_passes_a_canonical_model_id_through_unchanged(tmp_path, monkeypatch, model):
    """The CLI accepts these verbatim. Rewriting `claude-opus-4-8` to `opus` ran
    whatever is newest instead, so config.json named one model and the CLI ran
    another -- a lost pin with no error to notice it by."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model=model)).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", model]


def test_claude_cli_keeps_a_pin_that_differs_only_in_capitalisation(tmp_path, monkeypatch):
    """Capitalisation is not a reason to lose a pin: fold it to the canonical id
    rather than letting the alias path claim it."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model="CLAUDE-OPUS-4-8")).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", "claude-opus-4-8"]


def test_claude_cli_pins_the_id_for_every_call_shape(tmp_path, monkeypatch):
    """`--model` is assembled separately in the JSON-schema and plain-text paths;
    a pin that survived only one of them would still be lost on the other."""
    commands = _record_commands(monkeypatch)
    adapter = ClaudeCliAdapter(_cfg(tmp_path, model="claude-opus-4-8"))

    adapter.extract_facts(source_text="x")  # --json-schema path
    adapter.answer_question(question="Who?", context="c")  # plain-text path

    assert [c[0:3] for c in commands] == [["claude", "--model", "claude-opus-4-8"]] * 2


@pytest.mark.parametrize(
    ("display", "expected"),
    [("Opus 4.8", "opus"), ("Claude Opus", "opus"), ("claude-opus", "opus"), ("SONNET", "sonnet")],
)
def test_claude_cli_still_collapses_loose_display_names(tmp_path, monkeypatch, display, expected):
    """The looseness stays for names that are not ids -- only canonical ids are
    exempt, so pinning is not bought by breaking the display-name convenience."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model=display)).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", expected]


def test_claude_cli_surfaces_an_unknown_model_id_instead_of_substituting_one(tmp_path, monkeypatch):
    """Passing an id through means a wrong one reaches the CLI, which exits 1 with
    its own message. That loud failure is the point: the alternative was quietly
    running a model the user never chose."""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="There's an issue with the selected model (claude-nonexistent-9).",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="claude-nonexistent-9"):
        ClaudeCliAdapter(_cfg(tmp_path, model="claude-nonexistent-9")).extract_facts(source_text="x")


# --- one subprocess call site, shared by both command shapes ---


def test_claude_cli_routes_every_command_shape_through_one_call_site(tmp_path, monkeypatch):
    """The bug this refactor precedes (#474) was a missing `except` clause that had
    to be added twice because the body was copy-pasted twice. A shape that skips
    `_invoke` would keep its own copy of the clauses and drift again."""
    seen = []

    def recorder(self, cmd):
        seen.append(cmd)
        return '{"facts":[]}'

    monkeypatch.setattr(ClaudeCliAdapter, "_invoke", recorder)
    adapter = ClaudeCliAdapter(_cfg(tmp_path))

    adapter.extract_facts(source_text="x")  # --json-schema path
    adapter.answer_question(question="Who?", context="c")  # plain-text path

    assert len(seen) == 2
    assert "--json-schema" in seen[0]
    assert "--json-schema" not in seen[1]
