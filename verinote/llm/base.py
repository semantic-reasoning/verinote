# SPDX-License-Identifier: MPL-2.0
"""The provider-agnostic LLM contract."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from verinote.pipeline.query_intent import QueryIntent

FactSlotKind = Literal["string", "term"]


class LLMError(RuntimeError):
    """Any provider-side or parsing failure, normalised across adapters."""


# A secret shorter than this cannot be replaced without mangling ordinary text —
# a key of "key" would turn "invalid api key" into "invalid api ***" — so it is
# deliberately left alone. That is a statement about collateral damage, not about
# what counts as a credential: a short key (a self-hosted gateway's shared token,
# say) is NOT protected here. Error text that varies with the key's content is
# itself a small oracle, which is the other half of why the floor exists.
MIN_REDACTABLE_SECRET = 8


def redact_secret(text: str, secret: str | None) -> str:
    """Replace `secret` wherever it appears in `text`.

    Applied where an `LLMError` is *constructed*, not where one is stored. A
    provider's 401 body can echo the key it rejected, and that text reaches four
    different sinks — the settings banner, `source_chunks.error`, a question's
    failure reason, and the model-list error. Redacting at the sinks means
    enumerating sinks; redacting at construction means the secret never enters
    an `LLMError` in the first place, which is the same protected-by-construction
    argument `factory.get_client` makes for the corrupt-config halt.

    Substring replacement on the one string known to be secret, never a
    `sk-`-style pattern: key formats differ per vendor and self-hosted gateways
    invent their own, so a pattern both misses real keys and mangles innocent
    text. This does not cover a key echoed back *transformed* (encoded, hashed);
    that residue is why not shipping keys to caller-named endpoints is the
    primary control and this is defence in depth.
    """
    if not secret or len(secret) < MIN_REDACTABLE_SECRET:
        return text
    return text.replace(secret, "***")


def parsed_under_redaction(parse, payload, secret):
    """Run a response parser, and put what it raises under `redact_secret`.

    The parsers live below the adapters and take a payload, not a `Config`, so
    they cannot reach the key -- and they interpolate what they caught:
    `schema.parse_facts` puts the offending object in with `{item!r}`, which on
    a malformed response IS the response. `redact_secret` says the secret should
    never enter an `LLMError` in the first place; this is the construction site
    that has to call it for that to hold on the parse path.

    NOT inside the adapters' request `try`. Moving the parse in there would fix
    the redaction and break the attribution, reporting a schema failure as
    "request failed" -- the misattribution #500 took out of this very region.
    A separate wrapper redacts without moving the blame, the shape `_rendered`
    already has for the render.

    `from exc.__cause__`, not `from exc`. Every parser raises `... from exc`
    with the `json`/`Key`/`Type` error that caused it, so the original is
    already on `__cause__`; re-raising `from exc` would bury it behind this
    wrapper's own `LLMError`. Where a parser raised with no cause at all
    (`query translation was empty`), `__cause__` is `None` and the re-raise
    carries none either, which is what it had.

    ONLY `LLMError` is caught. A parser that fails some other way is a bug in
    this repo rather than a message about the provider's payload, and it is not
    this function's business to relabel it.
    """
    try:
        return parse(payload)
    except LLMError as exc:
        raise LLMError(redact_secret(str(exc), secret)) from exc.__cause__


def base_url_unusable(provider: str, url: str, exc: Exception) -> LLMError:
    """The configured endpoint is not something a request can even be built for.

    Nothing was dialled, so "request failed" would name the wrong suspect: the
    remote never heard from us and may be perfectly healthy. Naming the Base URL
    setting IS right here, and only here — `urllib.request.Request(url)` takes
    one caller-supplied string and rejects it for being that string, so there is
    no second cause to misdirect anyone towards. The cloud adapters' equivalent
    deliberately does not name the setting, because their SDK constructors read
    proxy and certificate environment too and a blank field is a live suspect
    there; see `_client_failed`.

    `url` is the string `Request` was actually handed — the configured base URL
    with this call's path already on it — and it is a parameter rather than
    something read back out of `exc` because the exception carries it only
    sometimes. Measured against `urllib.request.Request`:

        '::::/api/tags'     -> ValueError: unknown url type: '::::/api/tags'
        'http://[/api/tags' -> ValueError: Invalid IPv6 URL
        ''                  -> ValueError: unknown url type: ''

    The middle one is the whole argument for this parameter: told only "Invalid
    IPv6 URL", a user cannot see which of their settings produced it, and the
    message names a field without ever quoting what is in it.

    The URL is appended only where the exception has not already quoted it, so
    the ordinary case stays one sentence instead of saying the same string
    twice. The test is on `repr(url)`, not on `url`: `unknown url type` quotes
    with `!r`, so a URL holding anything `repr` escapes — a tab, a newline, a
    zero-width space or BOM carried along by a paste — is in that message in
    escaped form and not in raw form. Comparing raw would read "not mentioned"
    and append a second copy of a URL already on the line.

    Returns rather than raises, so the call site keeps `raise ... from exc` and
    the original stays in the chain — the shape `_request_failed` already uses.
    """
    hint = "check the Base URL setting"
    if f"{url!r}" not in str(exc):
        hint = f"tried {url!r}; {hint}"
    return LLMError(f"{provider} base URL is unusable: {exc} ({hint})")


@dataclass(frozen=True)
class ExtractedFact:
    """One candidate fact the extractor proposes from a source.

    Mirrors the `facts` columns the store will persist. The DuckDB-backed
    verifier and the human review gate decide whether it ever becomes engine input.
    """

    subject: str
    relation: str
    object: str
    confidence: float
    note: str = ""
    _: KW_ONLY
    subject_kind: FactSlotKind = "string"
    relation_kind: FactSlotKind = "string"
    object_kind: FactSlotKind = "string"

    def __post_init__(self) -> None:
        for name in ("subject_kind", "relation_kind", "object_kind"):
            value = getattr(self, name)
            if value not in {"string", "term"}:
                raise ValueError(f"{name} must be 'string' or 'term', got {value!r}")


@dataclass(frozen=True)
class ModelListing:
    """What one provider's model listing reported, for the settings picker.

    Every lister in `web.app._MODEL_LISTERS` returns this, so the dispatch has
    one return contract however many listers exist. Chosen over the two values
    (`list[str]` plus a parallel `frozenset[str] | None`) the design review also
    offered: those carry the same facts, but a bare pair is unpacked positionally,
    so swapping the two at one call site is a silent misgrouping rather than an
    error, and neither element can be named where a reader meets it.

    `models` is every id the listing returned, ordered.

    `structured_output_ids` is the subset the listing said advertises structured
    output, or `None` when the listing does not report that property at all.
    Those are different facts and the UI renders them differently: Ollama's
    `/api/tags` carries no such field, so `None` means "this listing does not
    say", while an empty frozenset means "it does say, and nothing advertised
    it". Collapsing the two would put every Ollama model under a heading
    claiming its server declared something it never mentioned.
    """

    models: tuple[str, ...]
    structured_output_ids: frozenset[str] | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Every provider adapter implements this; callers depend only on it.

    Timeout contract: every adapter MUST apply ``cfg.llm_timeout_seconds`` to
    each outbound call it makes -- the remote HTTP request for the cloud/local
    SDK adapters, or the subprocess run for the CLI adapter -- so no method can
    block indefinitely on a stuck provider. A bounded wait is part of the
    contract, not a per-adapter option.
    """

    name: str

    def extract_facts(self, *, source_text: str, schema_hint: str = "") -> list[ExtractedFact]:
        """Extract source-backed candidate facts from `source_text`.

        Adapters MUST force structured output (a JSON array of fact objects) and
        parse it into `ExtractedFact`s, raising `LLMError` on any provider error
        or schema violation so the caller can retry deterministically.
        """
        ...

    def translate_query(self, *, question: str, qid: int, schema_hint: str = "") -> str:
        """Translate an NL `question` into a single Datalog query line.

        Return a rule whose head is ``answer_q<qid>(V)`` binding one answer
        variable over ``relation/3``. When no executable rule is appropriate,
        return a durable non-executable outcome:
        ``review_required("reason")``, ``no_answer("reason")``, or
        ``ambiguous("reason")``. Raise `LLMError` on provider/parse error.
        """
        ...

    def extract_query_intent(self, *, question: str, schema_hint: str = "") -> "QueryIntent":
        """Extract a constrained query intent from `question`.

        This structured-output boundary is separate from Datalog translation.
        Adapters raise `LLMError` for malformed or schema-invalid intent output.
        """
        ...

    def answer_question(self, *, question: str, context: str) -> str:
        """Answer a free-form question from caller-provided context only.

        This is used by the read-only Ask workflow after deterministic engine
        routing cannot produce an executable answer. Adapters return plain text
        and raise `LLMError` for provider failures.
        """
        ...
