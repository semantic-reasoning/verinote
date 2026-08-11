# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the provider-agnostic helpers in `verinote.llm.base`.

The adapters' own suites cover these through a request; what is pinned here is
the part that has to hold identically for every caller, including the two
module-level `list_models` functions that hold no `Config` and no adapter.
"""

import urllib.request

import pytest

from verinote.llm.base import LLMError, base_url_unusable


def test_base_url_unusable_is_an_llm_error_naming_the_provider_and_the_setting():
    """Both halves are load-bearing. `LLMError` is what every caller catches
    (functional spec §10.1); the provider name is what the settings banner and a
    persisted chunk error use to say which of several configured endpoints broke.
    """
    err = base_url_unusable("ollama", ValueError("unknown url type: '::::'"))

    assert isinstance(err, LLMError)
    assert str(err) == (
        "ollama base URL is unusable: unknown url type: '::::' (check the Base URL setting)"
    )


def test_base_url_unusable_does_not_claim_the_request_failed():
    """"request failed" means a remote was dialled and did not cooperate. Nothing
    was dialled here, and a user reading that would go looking at a server that
    never heard from them."""
    assert "request failed" not in str(base_url_unusable("openrouter", ValueError("boom")))


def test_base_url_unusable_returns_rather_than_raises():
    """So the call site writes `raise base_url_unusable(...) from exc` and the
    original exception survives in `__cause__`. A helper that raised on its own
    would drop the chain unless every site remembered to rebuild it."""
    assert isinstance(base_url_unusable("ollama", ValueError("boom")), LLMError)


@pytest.mark.parametrize(
    "url",
    [
        "::::/api/tags",  # the settings-UI typo #493 was reported for
        "http://[/api/tags",  # an unclosed IPv6 literal
        "/api/tags",  # a path pasted where a URL was wanted
    ],
)
def test_the_urls_this_exists_for_really_do_raise_valueerror(url):
    """The narrow `except ValueError` at each call site is only correct if that
    is what `Request` actually raises. Measured here rather than assumed, so a
    Python release that changed the type would fail this instead of silently
    turning the new clauses into dead code."""
    with pytest.raises(ValueError):
        urllib.request.Request(url)
