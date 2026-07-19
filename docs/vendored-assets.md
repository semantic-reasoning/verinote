# Updating vendored assets

verinote ships one third-party asset in its own tree: `verinote/web/static/htmx.min.js`.
Nothing updates it for you. Dependabot and renovate read manifests, and a vendored
blob is not in any manifest — so no bot will ever tell you that htmx shipped a
security fix. This page is the manual procedure that replaces them, and the
cadence that decides when to run it.

## What is vendored, and why

htmx used to load from a CDN `<script>` tag. The review UI renders your document
text into the DOM, so unverified third-party code was executing in the same page
as your source material —
[#219](https://github.com/semantic-reasoning/verinote/issues/219). The fix was to
self-host: `verinote/web/templates/base.html` loads `/static/htmx.min.js`, and
[tests/test_base_template_assets.py](../tests/test_base_template_assets.py)
forbids any absolute-origin `src`/`href` in that template at all.

htmx is 0BSD-licensed, so vendoring the minified file carries no attribution
obligation beyond the source comment described below.

## Where the version and the hash live

**There is no manifest, no checksum file, and no SRI attribute.** Both facts live
in two adjacent lines at the top of `tests/test_base_template_assets.py`:

```python
# Source: htmx 2.0.3, https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js (0BSD).
HTMX_SHA256 = "491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e"
```

The comment is the only record of *which* htmx version is vendored — grep the
repository for a version number and you will find nothing else. The constant is
enforced by `test_vendored_htmx_bytes_match_the_pinned_hash`, which fails on any
byte change to the vendored file.

**Update both lines together.** Bumping the hash without the comment leaves the
repository with no record of what it is running.

## Updating

Replace `<VER>` with the target version throughout (for example `2.0.9`). Run
these from the repository root.

**1. Fetch the release artefact.** Prefer the official GitHub release asset over
a CDN mirror:

```sh
curl -sSL -o verinote/web/static/htmx.min.js \
  https://github.com/bigskysoftware/htmx/releases/download/v<VER>/htmx.min.js
```

**2. Cross-check against an independent mirror.** Two publishers agreeing on the
bytes is worth more than one publisher you trust. These two digests must match:

```sh
shasum -a 256 verinote/web/static/htmx.min.js
curl -sSL https://unpkg.com/htmx.org@<VER>/dist/htmx.min.js | shasum -a 256
```

If they disagree, **stop and investigate** — do not pick one. (For 2.0.3 the
GitHub release asset, unpkg, and jsDelivr are byte-identical.)

**3. Update the pin.** Edit `tests/test_base_template_assets.py`: set
`HTMX_SHA256` to the digest you just verified, and update the version and URL in
the `# Source:` comment above it.

**4. Verify.**

```sh
pytest -q tests/test_base_template_assets.py
pytest -q
ruff check .
```

**5. Check it by hand.** The tests prove the bytes are pinned and the file is
served; they do not prove htmx still works. Cut off network access, clear the
browser cache, run `verinote ui`, and accept something in the review queue. A
major-version jump can break htmx attributes that no test covers.

**6. Commit the bytes and the pin as one commit.** Splitting them leaves a commit
where the vendored file and `HTMX_SHA256` disagree, so the tree is red at that
revision and `git bisect` gets a false failure.

## When to check

- **Watch releases.** On
  [bigskysoftware/htmx](https://github.com/bigskysoftware/htmx), use Watch →
  Custom → Releases.
- **Watch for advisories.** The GitHub Advisory Database and `npm audit` against
  the `htmx.org` package are the two channels that would carry an htmx CVE. A
  security fix is the one reason to update out of band.
- **Check manually once a quarter.** Notifications get muted and filtered; the
  quarterly pass is what catches a release you never saw.

### Do not trust `/releases/latest`

htmx marks its prereleases with `prerelease: false`, so GitHub treats them as
stable. As of 2026-07-20 the API returns:

```sh
$ curl -sSL https://api.github.com/repos/bigskysoftware/htmx/releases/latest \
    | grep tag_name
  "tag_name": "v4.0.0-beta5",
```

**Read the tag names yourself and pick the newest 2.x stable release** — do not
let `/releases/latest`, or any tool built on it, choose the version.
