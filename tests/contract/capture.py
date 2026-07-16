# SPDX-License-Identifier: MPL-2.0
"""Capture real provider responses into replay fixtures for the #241 contract tests.

Run against a live provider, e.g. the local Claude Code CLI::

    VN_CONTRACT_PROVIDER=claudecli PYTHONPATH=$PWD \
        .venv/bin/python tests/contract/capture.py

For #237 and #238 this records the provider's *pre-parse* raw response — the
string the adapter hands to ``parse_query_intent`` / ``parse_facts`` — so the
replay tests exercise the same production parse boundary the live call does. It
grabs that string by wrapping the parser the adapter imported, which also lets
the capture succeed even when the raw response fails to parse (exactly the
failure shape #237/#238 are about).

For #239 no provider is needed: the failure reason is produced deterministically
by running the real chunked extraction pipeline with a stub client that raises
on every chunk, then reading the reason the store recorded — a genuine pipeline
artifact, not a hand-written string.

See ``README.md`` for the ollama/openai/anthropic capture procedure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

import verinote.llm.claude_cli_adapter as claude_cli_adapter
from verinote.config import Config
from verinote.llm.base import ExtractedFact, LLMError

CAPTURED_AT = "2026-07-16"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "contract"

# #237: a role question the deterministic parser deliberately does not resolve,
# so the live provider is the only thing that can produce an intent for it.
QUERY_INTENT_QUESTION = "Who is the CEO of Acme Robotics?"

# #238: a source that states two different founding dates for one entity. A
# correct extraction yields a founding-date fact whose relation normalises into
# the policy's functional vocabulary, which then trips the functional-conflict
# check on the two dates.
EXTRACTION_SOURCE = (
    "Acme Robotics is a company. Acme Robotics was founded in 2020 according to "
    "its incorporation filing. A later press release states that Acme Robotics "
    "was established in 2021."
)


def _live_config() -> Config:
    provider = os.environ.get("VN_CONTRACT_PROVIDER")
    if provider not in (None, "claudecli"):
        raise SystemExit(
            f"capture.py currently drives claudecli; got VN_CONTRACT_PROVIDER={provider!r}. "
            "See README.md for other providers."
        )
    root = Path(tempfile.mkdtemp(prefix="verinote-capture-"))
    return Config(
        root=root,
        db_path=root / "kb.sqlite",
        provider="claudecli",
        model="sonnet",
        api_key=None,
        base_url=None,
        llm_timeout_seconds=180.0,
    )


def _write(name: str, payload: dict) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(FIXTURES_DIR.parent.parent)}")


def _capture_raw(monkey_attr: str):
    """Wrap the parser the adapter imported so we keep the raw arg it is handed."""
    original = getattr(claude_cli_adapter, monkey_attr)
    box: dict[str, object] = {}

    def wrapper(raw, *args, **kwargs):
        box["raw"] = raw
        return original(raw, *args, **kwargs)

    setattr(claude_cli_adapter, monkey_attr, wrapper)
    return original, box


def capture_query_intent(cfg: Config) -> None:
    client = claude_cli_adapter.ClaudeCliAdapter(cfg)
    original, box = _capture_raw("parse_query_intent")
    parse_error = None
    try:
        client.extract_query_intent(question=QUERY_INTENT_QUESTION)
    except LLMError as exc:
        parse_error = str(exc)
    finally:
        claude_cli_adapter.parse_query_intent = original
    if "raw" not in box:
        raise SystemExit("query-intent capture never reached the parse boundary")
    _write(
        "query_intent_acme_ceo.json",
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "prompt_id": "query-intent",
            "captured_at": CAPTURED_AT,
            "input": QUERY_INTENT_QUESTION,
            "raw_response": box["raw"],
            "parse_error": parse_error,
        },
    )


def capture_extraction(cfg: Config) -> None:
    client = claude_cli_adapter.ClaudeCliAdapter(cfg)
    original, box = _capture_raw("parse_facts")
    parse_error = None
    try:
        client.extract_facts(source_text=EXTRACTION_SOURCE)
    except LLMError as exc:
        parse_error = str(exc)
    finally:
        claude_cli_adapter.parse_facts = original
    if "raw" not in box:
        raise SystemExit("extraction capture never reached the parse boundary")
    _write(
        "extraction_acme_two_dates.json",
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "prompt_id": "extraction",
            "captured_at": CAPTURED_AT,
            "input": EXTRACTION_SOURCE,
            "raw_response": box["raw"],
            "parse_error": parse_error,
        },
    )


class _AlwaysFailsClient:
    """A stub `LLMClient` that raises `LLMError` on every extraction chunk."""

    name = "always-fails"

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        raise LLMError("provider refused chunk: synthetic outage for #239 capture")


def capture_sync_failure() -> None:
    """Record the reason the chunked pipeline persists when every chunk fails.

    Deterministic and provider-free: it drives the real chunked extraction job,
    then reads back the failure reason the store recorded on the chunk.
    """
    from verinote.pipeline import create_chunked_extraction_job, process_extraction_job
    from verinote.store import Store

    root = Path(tempfile.mkdtemp(prefix="verinote-sync-capture-"))
    store = Store(root / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("sources/acme.txt", kind="text")
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        artifact_id=None,
        source_text=EXTRACTION_SOURCE,
        provider="always-fails",
        model="none",
        chunk_chars=40,
        chunk_overlap_chars=0,
    )
    process_extraction_job(store, _AlwaysFailsClient(), job_id=job_id)
    detail = store.get_extraction_job_detail(job_id)
    reasons = [
        row["error"]
        for row in store._conn.execute(  # noqa: SLF001 - capture script reads recorded reasons
            "SELECT error FROM source_chunks WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        if row["error"]
    ]
    store.close()
    if not reasons:
        raise SystemExit("sync-failure capture recorded no chunk failure reason")
    _write(
        "sync_all_chunks_failed.json",
        {
            "provider": "always-fails",
            "model": "none",
            "prompt_id": "sync-extraction",
            "captured_at": CAPTURED_AT,
            "input": EXTRACTION_SOURCE,
            "completed_chunks": int(detail["completed_chunks"]),
            "failed_chunks": int(detail["failed_chunks"]),
            "failure_reason": reasons[0],
        },
    )


def main() -> None:
    capture_sync_failure()
    cfg = _live_config()
    capture_query_intent(cfg)
    capture_extraction(cfg)


if __name__ == "__main__":
    main()
