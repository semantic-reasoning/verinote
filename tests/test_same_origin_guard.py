# SPDX-License-Identifier: MPL-2.0
"""The same-origin gate on state-changing requests.

`TestClient` sends neither `Origin` nor `Sec-Fetch-Site`, so every other test in
this suite lands in the fail-open branch and would pass with the middleware
deleted. Nothing here may rely on the bare client: each case sets the headers a
browser would, and the allow cases exist so a gate that simply refuses anything
carrying an `Origin` — which would break every real browser — cannot pass.
"""

import re
from urllib.parse import urlsplit

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import verinote.web.app as webapp  # noqa: E402
from verinote.config import Config  # noqa: E402
from verinote.engine import DEFAULT_POLICY  # noqa: E402
from verinote.pipeline.policy_state import POLICY_RELPATH, policy_sha256  # noqa: E402
from verinote.store import Store  # noqa: E402
from verinote.web import create_app  # noqa: E402

_SETTINGS_FORM = {
    "provider": "ollama",
    "model": "llama3.1",
    "base_url": "",
    "extraction_chunk_chars": "300",
    "extraction_chunk_overlap_chars": "40",
    "extraction_max_facts_per_chunk": "8",
}


def _client(tmp_path, *, with_policy: bool = True) -> TestClient:
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    with Store(cfg.db_path) as store:
        store.init_schema()
        store.add_fact("A", "is_a", "B", status="needs_review", confidence=0.9)
        policy = tmp_path / POLICY_RELPATH
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(DEFAULT_POLICY, encoding="utf-8")
        store.record_policy_marker(policy_sha256(DEFAULT_POLICY), origin="scaffold")
    if not with_policy:
        (tmp_path / POLICY_RELPATH).unlink()
    return TestClient(create_app(cfg), raise_server_exceptions=False)


# --- the attack this exists for ---


def test_cross_origin_post_is_refused_and_changes_nothing(tmp_path):
    """`POST /settings` stores an unvalidated base_url that `POST /settings/test`
    then dials carrying the API key. Refusing is only half of it — assert the
    write did not land, since a 403 rendered after the route ran would be worse
    than useless."""
    c = _client(tmp_path)
    before = c.app.state.cfg.provider

    r = c.post("/settings", data=_SETTINGS_FORM, headers={"Origin": "http://evil.example"})

    assert r.status_code == 403
    assert "cross-origin request refused" in r.text
    assert c.app.state.cfg.provider == before


def test_same_origin_post_still_works(tmp_path):
    """Pairs with the test above. Without this, "refuse anything with an Origin"
    would pass — and that breaks every real browser, which always sends one."""
    c = _client(tmp_path)

    r = c.post(
        "/settings",
        data=_SETTINGS_FORM,
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert c.app.state.cfg.provider == "ollama"


def test_the_gate_is_not_specific_to_settings(tmp_path):
    """Keyed on the method, not on a list of interesting paths — a fact decision
    taken by a page the user is not on is the same forgery."""
    c = _client(tmp_path)
    with Store(tmp_path / "kb.sqlite") as store:
        fact_id = store.facts()[0]["id"]

    r = c.post(f"/facts/{fact_id}/accept", headers={"Origin": "http://evil.example"})

    assert r.status_code == 403
    with Store(tmp_path / "kb.sqlite") as store:
        assert store.get_fact(fact_id)["status"] == "needs_review"


# --- the header decision table ---


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("same-origin", True), ("none", True), ("cross-site", False), ("same-site", False)],
)
def test_sec_fetch_site_decides_when_origin_is_absent(tmp_path, site, allowed):
    """`same-site` is refused deliberately: for an IP host every loopback port is
    same-site but cross-origin, so allowing it would let any other local dev
    server drive verinote."""
    c = _client(tmp_path)

    r = c.post(
        "/settings",
        data=_SETTINGS_FORM,
        headers={"Sec-Fetch-Site": site},
        follow_redirects=False,
    )

    assert (r.status_code != 403) is allowed


def test_opaque_origin_is_refused(tmp_path):
    """A sandboxed iframe sends `Origin: null`, which has no netloc and must not
    be allowed to match a missing or empty Host."""
    c = _client(tmp_path)

    r = c.post("/settings", data=_SETTINGS_FORM, headers={"Origin": "null"})

    assert r.status_code == 403


def test_a_client_sending_neither_header_is_allowed(tmp_path):
    """Documented as intentional, not an oversight: no browser page can omit both
    on a state-changing request, so this is curl or a script — which carries no
    ambient authority for the gate to protect."""
    c = _client(tmp_path)

    r = c.post("/settings", data=_SETTINGS_FORM, follow_redirects=False)

    assert r.status_code == 303


# --- reads, and the one GET that dials a caller-supplied endpoint ---


@pytest.mark.parametrize("path", ["/", "/review", "/settings"])
def test_cross_origin_reads_are_not_gated(tmp_path, path):
    """No CORS header is set anywhere, so a cross-origin GET is issued but
    unreadable. Gating it would 403 an ordinary inbound link for nothing."""
    c = _client(tmp_path)

    assert c.get(path, headers={"Origin": "http://evil.example"}).status_code == 200


def test_the_model_field_get_is_gated_because_it_dials_a_caller_url(tmp_path):
    """`?base_url=` names a host this GET will dial. What this guard stops is
    another site provoking that SSRF probe with no form and no interaction.

    It is not what keeps the probe keyless: `_MODEL_LISTERS` does that, by giving
    the listing seam a `(base_url, timeout)` signature that is handed no API key,
    and by building no client that holds one. That is narrower than "no way to
    hold a key" -- an import-time shape rule cannot stop a lister body from going
    and fetching one -- but the two are still independent controls over the same
    path and neither substitutes for the other: a keyless probe an attacker can
    still trigger is a real finding, and so is a same-origin-only request that
    could carry a key."""
    c = _client(tmp_path)
    url = "/settings/model-field?provider=ollama&base_url=http://evil.example"

    assert c.get(url, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_the_provider_change_url_the_page_actually_uses_is_gated(tmp_path):
    """The provider select's URL renders Model *plus* Base URL, and dials
    `?base_url=` exactly as the plain variant does -- so it has to be a query
    parameter on the listed path, not a route of its own. Read out of the shipped
    page rather than written here, because the mutation this must catch is a
    sibling route: `_ORIGIN_GUARD_GET_PATHS` matches on exact path or
    `path + "/"`, so `/settings/provider-fields` would sit outside it, and a
    hardcoded `/settings/model-field?provider_changed=1` in this test would go on
    passing while nothing in the browser used it.

    Mutation: serve the wrapper from a new ungated route -- the predicate goes
    False and the 403 becomes a 200, an ungated caller-URL dialer.

    The element is matched first and its `hx-get` read out of it, rather than one
    pattern spanning both: an attribute pattern anchored to `name="provider"`
    would go None the moment someone reorders the attributes in `settings.html`,
    which fails closed on the assert below but reports a template tidy-up as a
    guard failure.
    """
    c = _client(tmp_path)
    page = c.get("/settings").text
    select = re.search(r'<select\b[^>]*\bname="provider"[^>]*>', page)

    assert select is not None, "the settings page no longer has a provider select"
    hx_get = re.search(r'\bhx-get="([^"]+)"', select.group(0))
    assert hx_get is not None, "the provider select no longer has an hx-get to check"
    url = hx_get.group(1)
    assert webapp._origin_guard_applies("GET", urlsplit(url).path)
    probe = f"{url}{'&' if '?' in url else '?'}provider=ollama&base_url=http://evil.example"
    assert c.get(probe, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get(probe, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


# --- ordering, and what the reject path is allowed to touch ---


def test_the_gate_runs_before_the_policy_guard(tmp_path):
    """Middleware defined later runs first, which a reorder silently inverts. An
    unattributable request must not reach the policy guard: that reads the store
    and renders a 409, i.e. an attacker-triggerable oracle for the KB's state."""
    c = _client(tmp_path, with_policy=False)

    r = c.post("/settings", data=_SETTINGS_FORM, headers={"Origin": "http://evil.example"})

    assert r.status_code == 403  # not 409, the policy-halt page


def test_the_reject_path_reads_no_store(tmp_path, monkeypatch):
    """Rendering any template runs the shared context processor, which queries the
    store for a request already judged untrustworthy."""
    c = _client(tmp_path)
    monkeypatch.setattr(
        webapp.Store, "status_counts", lambda self: pytest.fail("store read on reject")
    )

    r = c.post("/settings", data=_SETTINGS_FORM, headers={"Origin": "http://evil.example"})

    assert r.status_code == 403


def test_an_opaque_origin_cannot_match_a_missing_host():
    """`urlsplit("null").netloc` is `""`, and a request with no Host header
    compares against `""` too — so without the emptiness guard the two would
    match and an opaque origin would be treated as same-origin. TestClient always
    sends a Host, so this branch is only reachable at the predicate."""
    from types import SimpleNamespace

    no_host = SimpleNamespace(headers={"origin": "null"})

    assert webapp._is_same_origin(no_host) is False


def test_a_different_port_on_the_same_host_is_cross_origin(tmp_path):
    """The load-bearing half of the `same-site` argument. Comparing hostnames
    instead of host:port is a natural "simplification" that would let any other
    local dev server drive verinote, and every other test here survives it."""
    c = _client(tmp_path)

    r = c.post("/settings", data=_SETTINGS_FORM, headers={"Origin": "http://testserver:9999"})

    assert r.status_code == 403
