# SPDX-License-Identifier: MPL-2.0
import json
from types import SimpleNamespace

import pytest

from verinote.config import Config, save_settings
from verinote.engine.terms import Compound, NumberLit
from verinote.llm.base import LLMError, LLMOutputError
from verinote.llm.ollama_adapter import OllamaAdapter, list_models
from verinote.llm.schema import FACT_ARRAY_SCHEMA
from verinote.pipeline import extract_source
from verinote.prompts import save_prompt_override
from verinote.store import Store


def _cfg(tmp_path, *, timeout: float = 900.0) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="ollama",
        model="qwen3:8b",
        api_key=None,
        base_url="http://localhost:11434",
        llm_timeout_seconds=timeout,
    )


class _Response:
    def __init__(self, content=None):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        content = self.content
        if content is None:
            content = json.dumps(
                {
                    "facts": [
                        {
                            "subject": {"kind": "string", "value": "Ada"},
                            "relation": {"kind": "string", "value": "is_a"},
                            "object": {"kind": "string", "value": "person"},
                            "confidence": 0.9,
                            "note": "",
                        }
                    ]
                }
            )
        return json.dumps(
            {
                "message": {
                    "content": content
                }
            }
        ).encode("utf-8")


def test_ollama_extract_uses_configured_timeout(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(req, *, timeout):
        calls.append(SimpleNamespace(req=req, timeout=timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    facts = OllamaAdapter(_cfg(tmp_path, timeout=900.0)).extract_facts(source_text="Ada")

    assert calls[0].timeout == 900.0
    payload = json.loads(calls[0].req.data.decode("utf-8"))
    assert payload["think"] is False
    assert payload["options"] == {"temperature": 0, "num_predict": 1800}
    assert payload["format"] == FACT_ARRAY_SCHEMA
    assert "document chunk" in payload["messages"][0]["content"]
    assert "up to 8 facts" in payload["messages"][0]["content"]
    assert "Do not summarize" in payload["messages"][0]["content"]
    assert "Traverse every visible section, table, list" in payload["messages"][0]["content"]
    assert "Do not sample representative rows" in payload["messages"][0]["content"]
    assert "Extract them row by row" in payload["messages"][0]["content"]
    assert "Use the row-identifying key" in payload["messages"][0]["content"]
    assert "semantic subject-predicate-object statement" in payload["messages"][0]["content"]
    assert "instead of copying whole source phrases" in payload["messages"][0]["content"]
    assert "merely because two entities appear in the same chunk" in payload["messages"][0]["content"]
    assert "same local evidence record" in payload["messages"][0]["content"]
    assert "`date(YYYY)`" in payload["messages"][0]["content"]
    assert "`amount(N,\"unit\")`" in payload["messages"][0]["content"]
    assert "Typed literals are object values, never subjects or relations" in payload["messages"][0]["content"]
    assert '`{"kind":"term","value":"..."}`' in payload["messages"][0]["content"]
    assert "relation `number(8)` and object `명`" in payload["messages"][0]["content"]
    assert "key-value or label-value text" in payload["messages"][0]["content"]
    assert "use relation `value`" in payload["messages"][0]["content"]
    assert "Do not use `is_a` unless" in payload["messages"][0]["content"]
    assert "Do not include source, status, CSV headers" in payload["messages"][0]["content"]
    assert facts[0].subject == "Ada"


def test_ollama_extract_preserves_explicit_compound_term_slots(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                {
                    "facts": [
                        {
                            "subject": {"kind": "string", "value": "Example Corp"},
                            "relation": {"kind": "string", "value": "founded_on"},
                            "object": {"kind": "term", "value": "date(2024,1,2)"},
                            "confidence": 0.9,
                            "note": "",
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    fact = OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Example Corp was founded on 2024-01-02.")[0]

    assert fact.object == "date(2024,1,2)"
    assert fact.object_kind == "term"

    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    assert (
        extract_source(
            store,
            OllamaAdapter(_cfg(tmp_path)),
            source_path="sources/example.txt",
            source_text="Example Corp was founded on 2024-01-02.",
        )
        == 1
    )
    stored = store.facts()[0]
    assert store.get_fact_terms(stored["id"])[2] == Compound(
        "date", (NumberLit(2024), NumberLit(1), NumberLit(2))
    )


def test_ollama_extract_raises_on_malformed_only_fact_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": None,
                        "confidence": 0.9,
                        "note": "",
                    }
                ]
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="malformed fact object"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_raises_on_schema_mismatch_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                {
                    "subject": "Ada",
                    "relation": "is_a",
                    "object": "mathematician",
                    "confidence": 0.9,
                    "note": "",
                }
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="extractor output did not match schema"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_raises_on_mixed_valid_and_malformed_payload(tmp_path, monkeypatch):
    def fake_urlopen(req, *, timeout):
        return _Response(
            json.dumps(
                [
                    {
                        "subject": "Ada",
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                    {
                        "subject": [],
                        "relation": "is_a",
                        "object": "mathematician",
                        "confidence": 0.9,
                        "note": "",
                    },
                ]
            )
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LLMError, match="malformed fact object"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_ollama_extract_uses_kb_prompt_override(tmp_path, monkeypatch):
    calls = []
    save_prompt_override(
        tmp_path,
        "ollama-extraction",
        "Custom local extraction prompt capped at {max_facts} facts.",
    )

    def fake_urlopen(req, *, timeout):
        calls.append(req)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")

    payload = json.loads(calls[0].data.decode("utf-8"))
    assert payload["messages"][0]["content"].startswith(
        "Custom local extraction prompt capped at 8 facts."
    )


def test_ollama_prompt_validation_error_is_llm_error(tmp_path):
    path = tmp_path / "policy" / "prompts" / "ollama-extraction.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing required placeholder.\n", encoding="utf-8")

    with pytest.raises(LLMError, match="\\{max_facts\\}"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def _record_request_url(monkeypatch) -> list:
    """Capture the URL actually requested, not the adapter's attribute."""
    urls = []

    def fake_urlopen(req, *, timeout):
        urls.append(req.full_url)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return urls


def test_empty_base_url_env_still_requests_localhost(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["http://localhost:11434/api/chat"]


def test_custom_base_url_env_reaches_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/v1")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


def test_trailing_slash_is_still_stripped(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "https://llm.internal/")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/api/chat"]


def test_settings_file_base_url_reaches_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="ollama", model="qwen3:8b", base_url="https://llm.internal/v1")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


def test_whitespace_only_saved_base_url_still_requests_localhost(tmp_path, monkeypatch):
    # Without normalising the saved source too, this requests '   /api/chat'.
    urls = _record_request_url(monkeypatch)
    monkeypatch.delenv("VERINOTE_BASE_URL", raising=False)
    save_settings(tmp_path, provider="ollama", model="qwen3:8b", base_url="   ")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["http://localhost:11434/api/chat"]


def test_padded_base_url_env_does_not_break_the_request_url(tmp_path, monkeypatch):
    urls = _record_request_url(monkeypatch)
    monkeypatch.setenv("VERINOTE_PROVIDER", "ollama")
    monkeypatch.setenv("VERINOTE_BASE_URL", "  https://llm.internal/v1  ")

    OllamaAdapter(Config.for_root(tmp_path)).extract_facts(source_text="Ada")

    assert urls == ["https://llm.internal/v1/api/chat"]


# --- what the request region normalises, and what it must not claim ---

# One invocation per LLM method. Each method builds and dials its own request,
# so a single-method test would leave three sites unguarded.
_INVOCATIONS = {
    "extract_facts": lambda a: a.extract_facts(source_text="Ada"),
    "translate_query": lambda a: a.translate_query(question="Who?", qid=1),
    "extract_query_intent": lambda a: a.extract_query_intent(question="What?"),
    "answer_question": lambda a: a.answer_question(question="Who?", context="c"),
}

_DATALOG = 'answer_q1(V) :- relation(V, "is_a", "x").'
_INTENT = {
    "kind": "unknown_or_unsupported",
    "subject": None,
    "relation": None,
    "object": None,
    "relation_candidates": None,
    "operator": None,
    "value_type": None,
    "value": None,
    "reason": "unsupported",
}

# What each method's parse accepts, so a stub can answer any of the four.
_VALID_CONTENT = {
    "extract_facts": json.dumps({"facts": []}),
    "translate_query": json.dumps({"datalog": _DATALOG}),
    "extract_query_intent": json.dumps(_INTENT),
    "answer_question": "ok",
}

# The three methods that parse the response *after* the request region closes.
# `answer_question` stringifies whatever came back and has no parse step, so it
# has no failure of that kind to mislabel — it is left out rather than given a
# case that could only ever pass.
_PARSING_INVOCATIONS = {
    name: call for name, call in _INVOCATIONS.items() if name != "answer_question"
}


class _RawResponse:
    """Answer with bytes the adapter has to decode and parse itself.

    `_Response` above always hands back a well-formed envelope built by
    `json.dumps`, so it can never exercise the decode and parse steps that sit
    inside the request region. This one can.
    """

    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return self.raw


def _raw_body(monkeypatch, raw: bytes) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda req, *, timeout: _RawResponse(raw))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_every_method_posts_through_the_one_request_site(tmp_path, monkeypatch, method):
    """All four methods go through `_post_chat`, so the guarded region below is
    written once and every case pinned here applies to all four.

    A method that grew its own inline `urlopen` again would still talk to a real
    server and satisfy every other test in this file; this is the one that would
    notice. `urlopen` is stubbed to fail so such a bypass is caught here instead
    of quietly passing against an Ollama actually running on the machine.
    """
    payloads = []

    def spy(self, payload):
        payloads.append(payload)
        return {"message": {"content": _VALID_CONTENT[method]}}

    monkeypatch.setattr(OllamaAdapter, "_post_chat", spy)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail(f"{method} dialled without going through _post_chat"),
    )

    _INVOCATIONS[method](OllamaAdapter(_cfg(tmp_path)))

    assert [payload["model"] for payload in payloads] == ["qwen3:8b"]


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_a_response_body_that_is_not_json_is_normalised_as_unusable_output(
    tmp_path, monkeypatch, method
):
    """A proxy or a captive portal answering HTML is normalised by this adapter,
    not a `JSONDecodeError` escaping it. Pinning that here means a later edit
    that folds the four request sites into one helper cannot quietly leave the
    decode outside the guard, which would turn this into an unnormalised raise.

    #592 RENAMED WHAT IT IS NORMALISED TO, and the rename is the substance. This
    used to join `urlopen` under one clause and report "ollama request failed",
    which says the server did not answer -- but it did: the response arrived,
    with a body, and the body is what could not be used. `translate_questions`
    reads that distinction off the class, so the old wording did more than
    misdirect a reader. Measured on a 200 carrying `<html>502 Bad Gateway</html>`
    it left the question row `pending` with no trace of the failure, where a
    `translation_failed` row is exactly true of it.
    """
    _raw_body(monkeypatch, b"not json")

    with pytest.raises(LLMOutputError, match="^ollama response could not be read"):
        _INVOCATIONS[method](OllamaAdapter(_cfg(tmp_path)))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_a_response_body_that_is_not_utf8_is_normalised_as_unusable_output(
    tmp_path, monkeypatch, method
):
    """`.decode("utf-8")` is the other statement that can raise on an otherwise
    well-formed HTTP response — a server that ignored the content type and sent
    UTF-16 gets here. Separate from the non-JSON case because the two are
    different statements, and an edit can move one without the other.
    """
    _raw_body(monkeypatch, b"\xff\xfe")

    with pytest.raises(LLMOutputError, match="^ollama response could not be read"):
        _INVOCATIONS[method](OllamaAdapter(_cfg(tmp_path)))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_a_request_that_never_landed_stays_a_plain_request_failure(
    tmp_path, monkeypatch, method
):
    """The counterweight to the two above, and the reason they are not vacuous.

    #592's discriminator is whether a response arrived, so `urlopen` failing and
    its body failing to decode must land on OPPOSITE sides -- otherwise one
    clause covering both would satisfy every assertion here. This one asserts the
    negative directly: a connection that never produced a body is an `LLMError`
    and is NOT an `LLMOutputError`, so merging the two clauses back together
    reddens this test whichever class the merged clause picks.
    """

    def refuse(req, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)

    with pytest.raises(LLMError, match="^ollama request failed") as exc:
        _INVOCATIONS[method](OllamaAdapter(_cfg(tmp_path)))
    assert not isinstance(exc.value, LLMOutputError)


@pytest.mark.parametrize("method", sorted(_PARSING_INVOCATIONS))
def test_a_schema_failure_is_not_reported_as_a_request_failure(tmp_path, monkeypatch, method):
    """The transport succeeded and the server answered valid JSON; the *model's*
    output did not match the schema. Calling that "request failed" sends the user
    to check an endpoint that answered fine — the misdirection #474 was reported
    as, arriving from the other direction.

    This is the boundary the region must NOT swallow, so it is the counterweight
    to the two cases above: widening the guard until it covers `parse_facts`
    would make both of those pass and this one fail.
    """
    _raw_body(monkeypatch, json.dumps({"message": {"content": "{}"}}).encode("utf-8"))

    with pytest.raises(LLMError) as exc:
        _PARSING_INVOCATIONS[method](OllamaAdapter(_cfg(tmp_path)))

    assert "request failed" not in str(exc.value)
    assert "did not match schema" in str(exc.value)


# --- a base URL no request can be built from ---

# The settings-UI typo #493 was reported for. `urllib.request.Request` rejects
# it with `ValueError: unknown url type` before any socket is opened.
_UNUSABLE_BASE_URL = "::::"


def _unusable_cfg(tmp_path) -> Config:
    """A Config whose Base URL cannot be turned into a request at all.

    Built here rather than by widening `_cfg`, so no existing test in this file
    changes shape for a case only these ones care about.
    """
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="ollama",
        model="qwen3:8b",
        api_key=None,
        base_url=_UNUSABLE_BASE_URL,
    )


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_an_unusable_base_url_is_normalised_before_anything_is_dialled(
    tmp_path, monkeypatch, method
):
    """A `base_url` typo is a settings-UI input, so this is reachable by typing.
    Unnormalised it escapes as a bare `ValueError`, which the web worker's
    generic handler turns into "analysis failed" and the CLI into a traceback.

    `urlopen` is stubbed to fail the test outright: "nothing was dialled" is half
    of what makes the message honest, and an assertion on the message alone would
    hold even if the adapter had opened a socket first.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("an unbuildable request must not reach the network"),
    )

    with pytest.raises(LLMError, match="^ollama base URL is unusable"):
        _INVOCATIONS[method](OllamaAdapter(_unusable_cfg(tmp_path)))


@pytest.mark.parametrize("method", sorted(_INVOCATIONS))
def test_an_unusable_base_url_keeps_the_original_error_as_the_cause(
    tmp_path, monkeypatch, method
):
    """`from exc`, so `unknown url type: '::::/api/chat'` — the part that says
    which URL and why — survives for a log even though the user-facing message
    is the shorter one."""
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError) as exc:
        _INVOCATIONS[method](OllamaAdapter(_unusable_cfg(tmp_path)))

    assert isinstance(exc.value.__cause__, ValueError)


# A Base URL whose rejection message says nothing about it: `Request` answers an
# unclosed IPv6 literal with a bare `Invalid IPv6 URL`. Distinct from `::::`,
# whose `unknown url type: '::::/api/chat'` quotes the URL for free — against
# that value alone, a call site that passed the wrong URL, or no URL at all,
# would look identical.
_SILENTLY_REFUSED_BASE_URL = "http://["


def test_the_url_reaches_the_message_even_when_the_exception_omits_it(tmp_path, monkeypatch):
    """What the user has to be given back is the string that was refused. The
    exception cannot supply it here, so `_post_chat` passes it — including the
    `/api/chat` this call appended, which is the URL `Request` actually saw.
    """
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="ollama",
        model="qwen3:8b",
        api_key=None,
        base_url=_SILENTLY_REFUSED_BASE_URL,
    )

    with pytest.raises(LLMError) as exc:
        OllamaAdapter(cfg).extract_facts(source_text="Ada")

    assert "Invalid IPv6 URL" in str(exc.value)
    assert "http://[/api/chat" in str(exc.value)


def test_a_payload_that_cannot_be_serialised_is_not_blamed_on_the_base_url(
    tmp_path, monkeypatch
):
    """`json.dumps(payload).encode(...)` used to be an ARGUMENT to `Request`,
    which means it would be evaluated inside the guarded region if left there.
    A `ValueError` raised while serialising is a bug in this file, and reporting
    it as "base URL is unusable" would send the user to edit a field that is
    perfectly fine — the same misattribution #474 was reported as.
    """
    monkeypatch.setattr(
        "json.dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("payload not serialisable"))
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(ValueError) as exc:
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")

    assert "base URL" not in str(exc.value)


def test_a_non_valueerror_from_the_request_constructor_is_not_blamed_on_the_base_url(
    tmp_path, monkeypatch
):
    """The clause catches `ValueError` and nothing wider. `Request` cannot raise
    a `TypeError` for any value reachable through settings — it stringifies
    whatever it is handed — so this stubs one in: the point is that widening the
    clause to `except Exception` would relabel a genuine programming error as a
    user configuration mistake, and no reachable input can demonstrate that.
    """

    def boom(*args, **kwargs):
        raise TypeError("Request() got an unexpected keyword argument")

    monkeypatch.setattr("urllib.request.Request", boom)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(TypeError):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


# --- list_models: the settings picker's source of truth ---


class _TagsResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _tags(monkeypatch, payload):
    """Answer /api/tags with `payload`; return the recorded (url, timeout) calls."""
    calls = []

    def fake_urlopen(req, *, timeout):
        calls.append(SimpleNamespace(url=req.full_url, timeout=timeout))
        return _TagsResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_list_models_returns_sorted_unique_names_from_tags(tmp_path, monkeypatch):
    calls = _tags(
        monkeypatch,
        {
            "models": [
                {"name": "qwen3:8b"},
                {"name": "llava:7b"},
                {"name": "  qwen3:8b  "},  # same model, padded
                {"name": "   "},  # no usable id
                {"no_name": 1},
                "not-an-object",
            ]
        },
    )

    listing = list_models(None, 5.0)

    assert listing.models == ("llava:7b", "qwen3:8b")
    assert calls[0].url == "http://localhost:11434/api/tags"


def test_list_models_honours_the_supplied_base_url(monkeypatch):
    calls = _tags(monkeypatch, {"models": []})

    list_models("http://llm.internal:9999/", 5.0)

    assert calls[0].url == "http://llm.internal:9999/api/tags"


def test_list_models_passes_the_supplied_timeout_through(monkeypatch):
    """The clamp against the generation budget belongs to the caller that still
    holds a Config; this function must not silently re-derive or override it."""
    calls = _tags(monkeypatch, {"models": []})

    list_models(None, 1.5)

    assert calls[0].timeout == 1.5


def test_list_models_takes_no_config_so_it_cannot_be_handed_a_key(monkeypatch):
    """The settings picker dials a caller-supplied URL, so a listing routine that
    could reach `cfg.api_key` would be one edit away from exfiltrating it. Taking
    no `Config` means no key is handed in, which is what this pins for Ollama's
    lister. It is not a guarantee that no key is reachable: a body can still go
    fetch one. `_check_every_listable_provider_has_a_keyless_lister` applies the
    same shape rule at import to every lister in the web layer's shipped table."""
    import inspect

    assert tuple(inspect.signature(list_models).parameters) == ("base_url", "timeout")
    assert not hasattr(OllamaAdapter, "list_models")


def test_list_models_returns_empty_for_a_server_with_nothing_pulled(monkeypatch):
    """Reachable-but-empty is data, not an error: the caller must be able to say
    'this server has no models' rather than 'this server could not be reached'."""
    _tags(monkeypatch, {"models": []})

    assert list_models(None, 5.0).models == ()


def test_list_models_reports_no_structured_output_opinion_at_all(monkeypatch):
    """`/api/tags` says nothing about capabilities, so this listing must not
    answer for it. `None` is 'this listing does not say'; the empty frozenset a
    listing that does report the property would return means 'it says none'.

    Mutation: return `frozenset()` here instead of `None` and the settings picker
    files every installed model under 'Does not advertise structured output' — a
    heading claiming the Ollama server declared something it never mentioned.
    """
    _tags(monkeypatch, {"models": [{"name": "qwen3:8b"}]})

    listing = list_models(None, 5.0)

    assert listing.structured_output_ids is None


def test_list_models_raises_on_transport_failure(monkeypatch):
    def boom(req, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    with pytest.raises(LLMError, match="ollama request failed"):
        list_models(None, 5.0)


@pytest.mark.parametrize("payload", [{"models": "qwen3:8b"}, {}, ["qwen3:8b"]])
def test_list_models_raises_on_schema_mismatch(monkeypatch, payload):
    """A shape that is not {'models': [...]} is a failure, never a silent empty
    list -- that would render as 'no models installed' and blame the user."""
    _tags(monkeypatch, payload)

    with pytest.raises(LLMError, match="did not match schema"):
        list_models(None, 5.0)


def test_list_models_normalises_a_base_url_no_request_can_be_built_from(monkeypatch):
    """The worst of the three sites: `GET /settings/model-field?base_url=::::`
    reaches this with a URL that was never saved, so a user only has to type into
    the Base URL box to get a 500 out of the settings page. `LLMError` is what
    that endpoint already knows how to render as a banner.
    """
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError, match="^ollama base URL is unusable"):
        list_models(_UNUSABLE_BASE_URL, 5.0)


def test_list_models_puts_the_refused_url_in_the_message(monkeypatch):
    """The lister's own call site, not the adapter's: it appends `/api/tags` and
    has to hand that same string over. `Invalid IPv6 URL` carries nothing, so a
    lister that forgot would leave the settings banner naming a field without
    ever saying what is in it."""
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError) as exc:
        list_models(_SILENTLY_REFUSED_BASE_URL, 5.0)

    assert "http://[/api/tags" in str(exc.value)


def test_list_models_does_not_blame_the_base_url_for_a_non_valueerror(monkeypatch):
    """The lister's clause is as narrow as the adapter's, for the same reason:
    `except Exception` here would report every future bug in this statement as
    the user's typo."""

    def boom(*args, **kwargs):
        raise TypeError("Request() got an unexpected keyword argument")

    monkeypatch.setattr("urllib.request.Request", boom)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(TypeError):
        list_models(None, 5.0)


# --- a prompt that cannot be rendered is still an `LLMError` (#500) ---


def _unreadable_override(tmp_path, prompt_id: str) -> None:
    """An override the render cannot decode.

    Bytes, not `save_prompt_override`: that helper validates and writes UTF-8, so
    it cannot produce this file. A missing required placeholder would be the
    cheaper break, but `ollama-extraction` is the only prompt on this adapter's
    `extract_facts` path with one, and a non-UTF-8 override breaks every prompt
    id regardless -- see the #500 section in `tests/test_cloud_adapters.py`,
    which parametrizes over four of them.
    """
    path = tmp_path / "policy" / "prompts" / f"{prompt_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8\n")


def test_a_prompt_that_cannot_be_read_is_a_normalised_failure(tmp_path, monkeypatch):
    """This hole is not one #500's hoist opens -- it is already open here.

    This adapter has always rendered outside its `try`, which is the shape #500
    asks the cloud adapters to adopt. So the `UnicodeDecodeError` that
    `_render_prompt`'s `except PromptError` does not convert leaves the adapter
    as itself, and §10.1 -- every LLM failure reaches its caller as an
    `LLMError` -- is violated here today, with nothing dialled. The cloud
    adapters were only hiding the same hole behind an argument-position render.
    """
    _unreadable_override(tmp_path, "ollama-extraction")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError, match="^prompt ollama-extraction could not be loaded"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")


def test_an_unlisted_render_failure_is_a_normalised_failure(tmp_path, monkeypatch):
    """Coverage that does not depend on an enumeration.

    The two failures reachable through a file today are `UnicodeDecodeError` and
    `PermissionError`, and #500's reviewer refused to treat that pair as
    complete: `render_prompt` reads two files and the `OSError` family they can
    raise is open. A clause narrowed back to a list of types passes the test
    above and fails this one.
    """

    class _Unlisted(Exception):
        pass

    def boom(*args, **kwargs):
        raise _Unlisted("nobody enumerated this")

    monkeypatch.setattr("verinote.llm.ollama_adapter.render_prompt", boom)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("dialled"))

    with pytest.raises(LLMError, match="^prompt ollama-extraction could not be loaded"):
        OllamaAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada")
