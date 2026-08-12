# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the provider-agnostic helpers in `verinote.llm.base`.

The adapters' own suites cover these through a request; what is pinned here is
the part that has to hold identically for every caller, including the two
module-level `list_models` functions that hold no `Config` and no adapter.
"""

import urllib.request

import pytest

from verinote.llm.base import LLMError, base_url_unusable


# Every URL the three call sites can be made to refuse, as a user could type it.
# `Invalid IPv6 URL` is the one that earns the `url` parameter: alone among these
# it comes back from `Request` with no trace of what was handed in.
_REFUSED_URLS = [
    "::::/api/tags",  # the settings-UI typo #493 was reported for
    "http://[/api/tags",  # an unclosed IPv6 literal
    "/api/tags",  # a path pasted where a URL was wanted
]


def _refused(url: str, provider: str = "ollama") -> tuple[LLMError, ValueError]:
    """Build the error from the ValueError `Request` really raises for `url`.

    Not from a hand-written `ValueError("...")`: every claim below is about how
    much of the URL survives into the message, and a stand-in exception would let
    this file decide that instead of CPython. `pytest.fail` rather than a skip if
    the URL is accepted — a URL that stopped raising would make the assertions
    that follow vacuous rather than inapplicable.
    """
    try:
        urllib.request.Request(url)
    except ValueError as exc:
        return base_url_unusable(provider, url, exc), exc
    pytest.fail(f"{url!r} no longer raises; the call sites' clauses would be dead code")


def test_base_url_unusable_is_an_llm_error_naming_the_provider_and_the_setting():
    """Both halves are load-bearing. `LLMError` is what every caller catches
    (functional spec §10.1); the provider name is what the settings banner and a
    persisted chunk error use to say which of several configured endpoints broke.
    """
    err, _ = _refused("::::/api/tags")

    assert isinstance(err, LLMError)
    assert str(err) == (
        "ollama base URL is unusable: unknown url type: '::::/api/tags' "
        "(check the Base URL setting)"
    )


def test_base_url_unusable_does_not_claim_the_request_failed():
    """"request failed" means a remote was dialled and did not cooperate. Nothing
    was dialled here, and a user reading that would go looking at a server that
    never heard from them."""
    err, _ = _refused("::::/models", provider="openrouter")

    assert "request failed" not in str(err)


def test_base_url_unusable_returns_rather_than_raises():
    """So the call site writes `raise base_url_unusable(...) from exc` and the
    original exception survives in `__cause__`. A helper that raised on its own
    would drop the chain unless every site remembered to rebuild it."""
    assert isinstance(base_url_unusable("ollama", "::::", ValueError("boom")), LLMError)


@pytest.mark.parametrize("url", _REFUSED_URLS)
def test_the_urls_this_exists_for_really_do_raise_valueerror(url):
    """The narrow `except ValueError` at each call site is only correct if that
    is what `Request` actually raises. Measured here rather than assumed, so a
    Python release that changed the type would fail this instead of silently
    turning the new clauses into dead code."""
    with pytest.raises(ValueError):
        urllib.request.Request(url)


@pytest.mark.parametrize("url", _REFUSED_URLS)
def test_the_refused_url_is_in_the_message_however_the_exception_words_itself(url):
    """The message names the Base URL setting, so it has to quote what is in it.
    Held for every refusable URL and not just the reported one, because which of
    these a user types is not something this code gets to choose.
    """
    err, _ = _refused(url)

    assert url in str(err)


def test_an_exception_that_omits_the_url_is_why_the_url_is_a_parameter():
    """The case a two-argument helper loses outright. `Request('http://[...')`
    raises a bare `Invalid IPv6 URL` — no URL, no quoting, nothing the user can
    match against the field they are being sent to check. Passing the URL in is
    the only way the message can carry it, and this is the test that says so.
    """
    url = "http://[/api/tags"
    err, exc = _refused(url)

    assert url not in str(exc)  # the exception alone cannot carry it
    assert url in str(err)
    assert "tried 'http://[/api/tags'" in str(err)


def test_a_url_the_exception_already_quotes_is_not_repeated():
    """The other branch, and the reason there are two: `unknown url type` quotes
    the URL itself, so appending it again would print the same string twice in
    one sentence for the case #493 was actually reported with.
    """
    url = "::::/api/tags"
    err, _ = _refused(url)

    assert str(err).count(url) == 1
    assert "tried" not in str(err)
