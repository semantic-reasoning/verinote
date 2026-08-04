# SPDX-License-Identifier: MPL-2.0
"""Contract guard for the OpenRouter catalogue the settings Model picker reads.

The picker is built from two things `GET {base_url}/models` returns: a string
``id`` per entry, and a ``supported_parameters`` list carrying
``structured_outputs`` for the entries that advertise it. Both are OpenRouter's
schema, not verinote's, so a rename upstream is invisible to the deterministic
suite -- which stubs the response and would stay green while every real user's
picker either failed to load or filed all 300-odd models under "does not
advertise structured output".

Two guards, because the two ways it can rot fail differently:

* a missing or retyped ``id`` / ``supported_parameters`` makes
  :func:`~verinote.llm.openrouter_adapter.list_models` raise, and the picker
  degrades to a text input with an error -- loud, but only for users.
* a renamed ``structured_outputs`` value raises nothing at all. The listing
  still parses; the grouping just silently becomes "none of them". That one
  needs a positive assertion that the string is still in the live data, which is
  why the second guard exists and does not simply re-run the first.

Both guards read one fetched snapshot, and the module makes exactly one request:
they assert about the same catalogue, and a comparison between them cannot fail
merely because OpenRouter edited its listing between two calls.

Gated opt-in like every other module here, so the default suite never reaches
the network; the scheduled provider lane is what runs it. It needs no API key
and builds no client -- the catalogue endpoint answers unauthenticated, which is
the property that lets the settings seam list models without holding a key.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from verinote.llm.openrouter_adapter import (
    OPENROUTER_DEFAULT_BASE_URL,
    _STRUCTURED_OUTPUTS_PARAMETER,
    list_models,
)

_TIMEOUT_SECONDS = 30.0
_raw_catalogue: dict | None = None


def _live_catalogue() -> dict:
    """The decoded catalogue body. The only request this module makes, once.

    Memoised rather than a module-scoped fixture: the gate is a function-scoped
    fixture, and a module-scoped one could not depend on it -- so the fetch would
    move above the gate and the default suite would hit the network on collection.
    """
    global _raw_catalogue
    if _raw_catalogue is None:
        req = urllib.request.Request(f"{OPENROUTER_DEFAULT_BASE_URL}/models")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            _raw_catalogue = json.loads(resp.read().decode("utf-8"))
    return _raw_catalogue


def _listing_from_the_fetched_catalogue(monkeypatch):
    """The production parse, run over the body `_live_catalogue` already fetched.

    `list_models` fetches and parses in one function, so handing its transport
    the snapshot is the only way to reach the parse with the same bytes the raw
    assertions were made against. Calling it plainly would dial the endpoint a
    second time, and two responses need not agree: OpenRouter edits the
    catalogue continuously and serves it from more than one backend, so
    comparing a parse of snapshot B against ids read out of snapshot A reddens
    the scheduled lane for an ordinary upstream edit with no contract broken.

    Only the second *request* is stubbed. The bytes are the live ones, and every
    line of `list_models` that reads them -- the schema checks, the id
    normalisation, the `supported_parameters` split -- runs unchanged, which is
    what these guards are here to exercise.
    """
    body = _live_catalogue()

    class _Snapshot:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self):
            return json.dumps(body).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, *, timeout: _Snapshot())
    return list_models(None, _TIMEOUT_SECONDS)


@pytest.mark.contract
def test_live_catalogue_entries_carry_an_id_and_supported_parameters(require_opt_in):
    """Every entry must still have the two fields the picker is built from.

    Asserted against the raw body, not through `list_models`: the adapter raises
    on the first bad entry, so a partial rename would surface as one opaque
    error instead of a count. Here it names how many entries broke and which.
    """
    body = _live_catalogue()
    assert isinstance(body, dict) and isinstance(body.get("data"), list), (
        f"the catalogue is no longer {{'data': [...]}}: {sorted(body)[:8]}"
    )
    entries = body["data"]
    assert entries, "the live catalogue listed no models at all"
    malformed = [
        entry.get("id", entry)
        for entry in entries
        if not isinstance(entry, dict)
        or not isinstance(entry.get("id"), str)
        or not isinstance(entry.get("supported_parameters"), list)
    ]
    assert not malformed, (
        f"{len(malformed)}/{len(entries)} catalogue entries lack a string `id` or a "
        f"`supported_parameters` list: {malformed[:5]}"
    )


@pytest.mark.contract
def test_live_catalogue_still_declares_structured_outputs_and_splits_in_two(
    require_opt_in, monkeypatch
):
    """The grouping value must still appear, and must still split the catalogue.

    A rename of `structured_outputs` raises nothing -- the listing parses and
    every model quietly lands in "does not advertise structured output" -- so the
    presence of the string is asserted positively. Both groups are then required
    to be non-empty through the production `list_models`, because a picker whose
    second group is always empty renders a heading that never means anything, and
    one whose first group is empty is the silent-rename failure itself.

    Both halves read the one fetched snapshot, so the equality below compares a
    parse against the very bytes it parsed rather than against a second, possibly
    newer response -- see `_listing_from_the_fetched_catalogue`.
    """
    entries = _live_catalogue()["data"]
    declaring = [
        entry["id"]
        for entry in entries
        if _STRUCTURED_OUTPUTS_PARAMETER in entry.get("supported_parameters", [])
    ]
    assert declaring, (
        f"no live catalogue entry lists {_STRUCTURED_OUTPUTS_PARAMETER!r} in its "
        "`supported_parameters`; either OpenRouter renamed it or nothing advertises "
        "it any more, and the settings picker's grouping now says the same thing "
        "about every model"
    )

    listing = _listing_from_the_fetched_catalogue(monkeypatch)

    assert listing.structured_output_ids is not None
    assert set(listing.structured_output_ids) == set(declaring)
    assert set(listing.models) - set(listing.structured_output_ids), (
        "every live model advertises structured output, so the picker's second "
        "group is empty; the split is no longer telling a user anything"
    )
