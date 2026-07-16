# SPDX-License-Identifier: MPL-2.0
"""Contract guard for issue #239: `verinote sync` must not report success when
every extraction chunk fails.

Deterministic and provider-free — it drives the real chunked-extraction CLI path
with a stub client that raises on each chunk, so it needs no live provider. It is
still gated behind the same opt-in signal as the live guards (via
``require_opt_in``) so the default suite stays green; without that gate a run of
``pytest tests`` would go red on a bug this branch has not fixed yet.

On ``origin/main`` (no #239 fix) the chunked path swallows every per-chunk
``LLMError`` and `cmd_sync` returns 0, so both assertions here are expected to
fail. That red is the point; ``@pytest.mark.contract`` keeps it opt-in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import verinote.cli as cli
from verinote.llm.base import ExtractedFact, LLMError

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "contract" / "sync_all_chunks_failed.json"

# Long enough, with a tiny chunk size, to split into several chunks so "every
# chunk failed" is a real multi-chunk annihilation, not a single-chunk edge case.
_SOURCE = (
    "Acme Robotics is a company. Acme Robotics was founded in 2020 according to "
    "its incorporation filing. A later press release states that Acme Robotics "
    "was established in 2021. The company builds warehouse automation systems. "
    "Its headquarters are in Portland. It employs three hundred people."
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _AllChunksFail:
    """Raises `LLMError` on every chunk — the provider-down / all-fail scenario."""

    name = "all-chunks-fail"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        raise LLMError(self._reason)


class _FirstChunkFails:
    """Fails only the first chunk, then succeeds — the partial-failure scenario."""

    name = "first-chunk-fails"

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._calls = 0

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        self._calls += 1
        if self._calls == 1:
            raise LLMError(self._reason)
        return [ExtractedFact("Acme Robotics", "is_a", "company", 0.9)]


def _prepare_kb(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("VERINOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("VERINOTE_PROVIDER", "anthropic")
    # Force several chunks so all-fail is a genuine multi-chunk wipeout.
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_CHARS", "40")
    monkeypatch.setenv("VERINOTE_EXTRACTION_CHUNK_OVERLAP_CHARS", "0")
    src = tmp_path / "acme.txt"
    src.write_text(_SOURCE, encoding="utf-8")
    assert cli.main(["ingest", str(src)]) == 0
    return src


@pytest.mark.contract
def test_sync_fails_when_every_chunk_fails(tmp_path, monkeypatch, capsys, require_opt_in):
    reason = _fixture()["failure_reason"]
    assert reason, "fixture is missing the recorded chunk failure reason"
    _prepare_kb(tmp_path, monkeypatch)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _AllChunksFail(reason))

    rc = cli.main(["sync"])

    err = capsys.readouterr().err
    assert rc != 0, "#239 guard: sync must not return success when every chunk failed"
    assert "fail" in err.lower(), "#239 guard: the failure must be reported on stderr"


@pytest.mark.contract
def test_total_failure_differs_from_partial_failure(tmp_path, monkeypatch, capsys, require_opt_in):
    """Total wipeout and partial failure must not be reported identically (#239)."""
    reason = _fixture()["failure_reason"]

    _prepare_kb(tmp_path, monkeypatch)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _AllChunksFail(reason))
    total_rc = cli.main(["sync"])
    capsys.readouterr()

    _prepare_kb(tmp_path, monkeypatch)
    monkeypatch.setattr("verinote.llm.get_client", lambda cfg: _FirstChunkFails(reason))
    partial_rc = cli.main(["sync"])
    capsys.readouterr()

    assert total_rc != partial_rc, (
        "#239 guard: a total extraction wipeout must be distinguishable from a "
        f"partial failure, but both returned rc={total_rc}"
    )
