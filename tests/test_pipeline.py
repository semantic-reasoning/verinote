# SPDX-License-Identifier: MPL-2.0
import pytest

from verinote.llm.base import ExtractedFact, LLMError
from verinote.pipeline import extract_source, sync_sources
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_extract_source_persists_candidates_with_linkage(tmp_path, fake_client):
    s = _store(tmp_path)
    run_id = s.add_run(provider="fake", model="m")
    client = fake_client(
        [ExtractedFact("A", "is_a", "B", 0.9, "n"), ExtractedFact("C", "rel", "D", 0.8)]
    )

    n = extract_source(
        s, client, source_path="sources/x.txt", source_text="...", run_id=run_id
    )

    assert n == 2
    facts = s.facts()
    assert [f["status"] for f in facts] == ["candidate", "candidate"]
    assert {f["run_id"] for f in facts} == {run_id}
    assert {f["source_path"] for f in facts} == {"sources/x.txt"}


def test_sync_sources_opens_run_and_summarises(tmp_path, fake_client):
    s = _store(tmp_path)
    client = fake_client([ExtractedFact("A", "is_a", "B", 0.9)])

    result = sync_sources(
        s,
        client,
        [("sources/a.txt", "ta"), ("sources/b.txt", "tb")],
        provider="fake",
        model="m",
    )

    assert result.total == 2
    assert result.per_source == [("sources/a.txt", 1), ("sources/b.txt", 1)]
    assert client.calls == 2

    run = s.get_run(result.run_id)
    assert run["provider"] == "fake" and run["model"] == "m"
    assert run["summary"] == "2 source(s), 2 candidate(s)"
    # every fact cites the one run that produced it
    assert {f["run_id"] for f in s.facts()} == {result.run_id}


def test_sync_sources_propagates_llm_error(tmp_path, fake_client):
    s = _store(tmp_path)
    client = fake_client(error=LLMError("no api key"))

    with pytest.raises(LLMError):
        sync_sources(s, client, [("sources/a.txt", "t")], provider="fake", model="m")
