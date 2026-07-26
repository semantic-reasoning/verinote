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
import json
import re
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from verinote.web.app import create_app

WEB = Path(__file__).resolve().parents[1] / "verinote" / "web"
BASE_TEMPLATE = WEB / "templates" / "base.html"
VENDORED_HTMX = WEB / "static" / "htmx.min.js"
VENDORED_HTMX_METADATA = WEB / "static" / "htmx.min.js.metadata.json"

REQUIRED_HTMX_METADATA_KEYS = frozenset(
    {
        "version",
        "sha256",
        "tag",
        "tag_commit",
        "raw_url",
        "npm_package",
        "npm_tarball_url",
    }
)

# `src="..."` / `href="..."` whose value starts with an absolute http(s) origin,
# either quote style. Protocol-relative `//cdn...` is caught too via the optional scheme.
EXTERNAL_ASSET = re.compile(
    r"""\b(?:src|href)\s*=\s*["'](?:https?:)?//""",
    re.IGNORECASE,
)


def _base_html() -> str:
    return BASE_TEMPLATE.read_text(encoding="utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _htmx_metadata() -> dict[str, str]:
    assert VENDORED_HTMX_METADATA.is_file(), (
        f"missing htmx metadata at {VENDORED_HTMX_METADATA}"
    )
    try:
        metadata = json.loads(
            VENDORED_HTMX_METADATA.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise AssertionError(f"htmx metadata is not valid JSON: {error}") from error

    assert isinstance(metadata, dict), "htmx metadata must be a JSON object"
    assert set(metadata) == REQUIRED_HTMX_METADATA_KEYS, (
        "htmx metadata keys must be exactly "
        f"{sorted(REQUIRED_HTMX_METADATA_KEYS)}, got {sorted(metadata)}"
    )
    assert all(isinstance(value, str) and value for value in metadata.values()), (
        "htmx metadata values must all be non-empty JSON strings"
    )
    return metadata


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


def test_vendored_htmx_metadata_has_consistent_provenance() -> None:
    """The public provenance record must identify one coherent htmx release."""
    metadata = _htmx_metadata()
    version = metadata["version"]
    tag = metadata["tag"]

    assert re.fullmatch(r"2\.\d+\.\d+", version), (
        f"htmx version must be a final 2.x.y release, got {version!r}"
    )
    assert tag == f"v{version}", f"htmx tag {tag!r} does not match version {version!r}"
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["tag_commit"]), (
        "htmx tag_commit must be a 40-character lowercase Git commit SHA"
    )
    assert metadata["raw_url"] == (
        f"https://raw.githubusercontent.com/bigskysoftware/htmx/{tag}/dist/htmx.min.js"
    )
    assert metadata["npm_package"] == f"htmx.org@{version}"
    assert metadata["npm_tarball_url"] == (
        f"https://registry.npmjs.org/htmx.org/-/htmx.org-{version}.tgz"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]), (
        "htmx sha256 must be a 64-character lowercase SHA-256 digest"
    )


def test_vendored_htmx_bytes_match_metadata_hash() -> None:
    """Pin the exact bytes of the vendored file (supply-chain integrity).

    `"htmx" in body` would pass for a tampered file or a different htmx version.
    Since #219 is precisely about not trusting a remote CDN, the vendored artefact
    itself is pinned by SHA256; any change to its bytes must be a deliberate,
    reviewed metadata update.
    """
    metadata = _htmx_metadata()
    digest = hashlib.sha256(VENDORED_HTMX.read_bytes()).hexdigest()
    assert digest == metadata["sha256"], (
        f"vendored htmx.min.js SHA256 is {digest}, expected {metadata['sha256']}. "
        "If this is an intentional htmx update, update its metadata to match."
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


def test_vendored_htmx_metadata_is_served_as_json_from_the_static_mount() -> None:
    """The public provenance record must be available from the running application."""
    with TestClient(create_app(None)) as client:
        response = client.get("/static/htmx.min.js.metadata.json")
    assert response.status_code == 200, (
        "/static/htmx.min.js.metadata.json did not serve "
        f"(status {response.status_code})"
    )
    content_type = response.headers.get("content-type", "")
    assert "application/json" in content_type, (
        "/static/htmx.min.js.metadata.json served with content-type "
        f"{content_type!r}, expected JSON"
    )
    assert response.json() == _htmx_metadata()
