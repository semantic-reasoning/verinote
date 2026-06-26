# SPDX-License-Identifier: MPL-2.0
import subprocess
from types import SimpleNamespace

import pytest

from verinote.config import Config
from verinote.llm.base import LLMError
from verinote.llm.claude_cli_adapter import ClaudeCliAdapter
from verinote.llm.factory import get_client


def _cfg(tmp_path, *, model: str = "") -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claude",
        model=model,
        api_key=None,
        base_url=None,
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
    assert calls[0][0][0:2] == ["claude", "-p"]
    assert calls[0][1]["capture_output"] is True


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
    assert line.startswith("answer_q7")


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
