# SPDX-License-Identifier: MPL-2.0
"""Regression lock on base.html's asset origins (#219).

base.html used to pull htmx from `https://unpkg.com/htmx.org@2.0.3` with no
integrity attribute: a page render depended on a third party staying up, and a
cold cache meant the app did not work offline. The fix vendors htmx under
`verinote/web/static/` and points the tag at the local `/static` mount.

Asserting only "no `unpkg` string" would be too weak -- swapping unpkg for
another CDN (jsdelivr, cdnjs, ...) would sail through. So the origin guard
rejects *any* absolute `http(s)://` asset URL in base.html, and a second check
pins the local htmx load, and a third check pins that the vendored file exists
and is real htmx (not an empty or error placeholder that would 404-at-runtime
while the template still read as "self-hosted").
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from starlette.testclient import TestClient

from verinote.web.app import create_app

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
BASE_TEMPLATE = WEB / "templates" / "base.html"
VENDORED_HTMX = WEB / "static" / "htmx.min.js"

# SHA256 of the vendored file, pinned so a tampered or version-swapped htmx fails.
# Source: htmx 2.0.3, https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js (0BSD).
HTMX_SHA256 = "491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e"

# `src="..."` / `href="..."` whose value starts with an absolute http(s) origin,
# either quote style. Protocol-relative `//cdn...` is caught too via the optional scheme.
EXTERNAL_ASSET = re.compile(
    r"""\b(?:src|href)\s*=\s*["'](?:https?:)?//""",
    re.IGNORECASE,
)


def _base_html() -> str:
    return BASE_TEMPLATE.read_text(encoding="utf-8")


def test_base_template_loads_no_external_assets() -> None:
    """No asset in base.html may come from an absolute (off-origin) URL.

    Reverting base.html to the unpkg `<script>` -- or reintroducing any other CDN
    -- puts back an absolute origin here and fails this test.
    """
    offenders = [
        line.strip()
        for line in _base_html().splitlines()
        if EXTERNAL_ASSET.search(line)
    ]
    assert not offenders, (
        f"base.html loads assets from an external origin: {offenders}. "
        "Vendor them under verinote/web/static/ and load via /static instead."
    )


def test_base_template_loads_htmx_from_static() -> None:
    """htmx must be loaded from the local /static mount, not merely absent."""
    html = _base_html()
    assert re.search(
        r"""<script\b[^>]*\bsrc\s*=\s*["']/static/htmx\.min\.js["']""",
        html,
    ), "base.html does not load htmx from /static/htmx.min.js"


def test_vendored_htmx_file_exists_and_is_real() -> None:
    """The self-hosted file must exist and actually be htmx.

    A missing or empty file would 404 at runtime while the template still read as
    self-hosted, so pin the file's presence and an htmx signature, not just the tag.
    """
    assert VENDORED_HTMX.is_file(), f"missing vendored htmx at {VENDORED_HTMX}"
    body = VENDORED_HTMX.read_text(encoding="utf-8")
    assert body.strip(), "vendored htmx.min.js is empty"
    assert "htmx" in body, "vendored htmx.min.js does not look like htmx"


def test_vendored_htmx_bytes_match_the_pinned_hash() -> None:
    """Pin the exact bytes of the vendored file (supply-chain integrity).

    `"htmx" in body` would pass for a tampered file or a different htmx version.
    Since #219 is precisely about not trusting a remote CDN, the vendored artefact
    itself is pinned by SHA256; any change to its bytes must be a deliberate,
    reviewed update to HTMX_SHA256.
    """
    digest = hashlib.sha256(VENDORED_HTMX.read_bytes()).hexdigest()
    assert digest == HTMX_SHA256, (
        f"vendored htmx.min.js SHA256 is {digest}, expected {HTMX_SHA256}. "
        "If this is an intentional htmx update, bump HTMX_SHA256 to match."
    )


def test_vendored_htmx_is_served_from_the_static_mount() -> None:
    """The /static mount must actually serve the file (the text checks can't prove this).

    Every other test reads static files; this one exercises the running app so a
    broken or renamed mount is caught, not just a correct template string.
    """
    with TestClient(create_app(None)) as client:
        response = client.get("/static/htmx.min.js")
    assert response.status_code == 200, (
        f"/static/htmx.min.js did not serve (status {response.status_code})"
    )
    content_type = response.headers.get("content-type", "")
    assert "javascript" in content_type, (
        f"/static/htmx.min.js served with content-type {content_type!r}, expected a JS type"
    )
