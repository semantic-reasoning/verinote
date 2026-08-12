# SPDX-License-Identifier: MPL-2.0
"""OpenRouter adapter — one endpoint that routes to many vendors' models.

OpenRouter speaks the OpenAI wire protocol, so every generation path is
inherited from `OpenAIAdapter` unchanged. What this module adds is the part
that is *not* the protocol: an endpoint bound to the provider the user picked
rather than to a text field they can leave blank, and the catalogue that
endpoint serves — `GET {base_url}/models`, keyless, which is why the settings
Model field can be a picker here and not for the other cloud providers.

That binding is why this is a provider of its own instead of a documented
`openai` + `base_url` recipe. Under the recipe an unset `base_url` resolves to
`api.openai.com`, so a user who chose OpenRouter and cleared the field would
ship their documents to a vendor they never selected — the confidentiality
failure `assert_settings_intact` exists to prevent (#269), arriving through
ordinary configuration instead of corruption. `OllamaAdapter` binds its
endpoint the same way and for the same reason.
"""

from __future__ import annotations

import json
import urllib.request

from verinote.llm.base import LLMError, ModelListing, base_url_unusable
from verinote.llm.openai_adapter import OpenAIAdapter

# The endpoint an unset `base_url` resolves to. Named rather than inlined so the
# settings surface reports the same URL this adapter dials, and so the web
# layer's per-provider default-endpoint map cannot drift from it.
OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# What a catalogue entry lists in `supported_parameters` when it declares
# structured output. Named because it is the string the settings picker's two
# groups are built from, and a rename upstream must change one place here and
# one assertion in `tests/contract/test_openrouter_catalogue_contract.py`.
_STRUCTURED_OUTPUTS_PARAMETER = "structured_outputs"


def list_models(base_url: str | None, timeout: float) -> ModelListing:
    """Model ids OpenRouter's catalogue lists, sorted, with the advertising subset.

    A module-level function taking a URL and a timeout, deliberately NOT a
    method on `OpenRouterAdapter`, for the reason `ollama_adapter.list_models`
    states: the settings picker dials an endpoint the caller supplied in a query
    string, and a method would have `self.cfg.api_key` in reach — so "this
    listing sends no key" would be a property of what the body happens to read
    today. It matters more here than it does for Ollama, because `openrouter` IS
    a key-holding provider (`PROVIDERS_REQUIRING_KEY`), so there is a real key to
    leak. No `Authorization` header is built below, and none needs to be: the
    catalogue endpoint answers unauthenticated, so nothing is traded for the
    omission. A later body edit *could* still add one, which is the residual the
    next sentence is about: the web layer checks that shape at import for every
    lister in its shipped table — not at dispatch, and it constrains only what a
    lister is handed, never what a body can reach for on its own; see
    `_MODEL_LISTERS`.

    Because the request carries no key, what comes back is the published
    catalogue, NOT the set of models the user's account may call. The settings
    note says so; do not let a caller quietly restate it as availability.

    `structured_output_ids` reports which entries listed
    `structured_outputs` among their `supported_parameters` — a repetition of what
    the catalogue declares, not a measurement: nothing here runs a model.

    Raises `LLMError` on any transport or shape failure rather than returning an
    empty listing — an empty catalogue means "this endpoint listed nothing", and
    a caller must be able to tell that apart from "the endpoint could not be
    reached" (which the settings UI reports verbatim instead of showing an empty
    picker). An entry whose `supported_parameters` is missing or not a list is a
    shape failure for the same reason: it cannot be grouped, and dropping it into
    "does not advertise" would report a claim the catalogue never made.
    """
    root = (base_url or OPENROUTER_DEFAULT_BASE_URL).rstrip("/")
    # Same reachable path as the Ollama lister: the settings picker dials the
    # endpoint currently in the Base URL box, so a malformed one arrives here
    # before it is ever saved. Escaping unnormalised makes that a 500 rather
    # than the banner the settings page already renders for `LLMError`.
    url = f"{root}/models"
    try:
        req = urllib.request.Request(url)
    except ValueError as exc:
        raise base_url_unusable("openrouter", url, exc) from exc
    try:
        with urllib.request.urlopen(  # noqa: S310 - caller-named endpoint, dialled with no key
            req, timeout=timeout
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - normalise provider/transport errors
        raise LLMError(f"openrouter request failed: {exc}") from exc

    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise LLMError("openrouter model list did not match schema: expected {'data': [...]}")
    ids: set[str] = set()
    advertising: set[str] = set()
    for entry in body["data"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not entry["id"].strip()
        ):
            raise LLMError(
                "openrouter model list did not match schema: an entry has no string id"
            )
        model_id = entry["id"].strip()
        parameters = entry.get("supported_parameters")
        if not isinstance(parameters, list):
            raise LLMError(
                "openrouter model list did not match schema: "
                f"{model_id} has no supported_parameters list"
            )
        ids.add(model_id)
        if _STRUCTURED_OUTPUTS_PARAMETER in parameters:
            advertising.add(model_id)
    return ModelListing(models=tuple(sorted(ids)), structured_output_ids=frozenset(advertising))


class OpenRouterAdapter(OpenAIAdapter):
    """OpenAI-protocol generation against OpenRouter.

    `name` is not cosmetic: the inherited request paths raise
    `LLMError(f"{self.name} request failed: ...")`, and that string is what the
    settings banner shows and what a failed extraction persists as its chunk
    error. Left at `openai` it would name the wrong provider in the one place a
    user looks to find out which provider broke.
    """

    name = "openrouter"

    def _base_url(self) -> str:
        return self.cfg.base_url or OPENROUTER_DEFAULT_BASE_URL
