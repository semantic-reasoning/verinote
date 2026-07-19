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

That comment is the only record of *which* htmx version is currently vendored,
and the constant is enforced by `test_vendored_htmx_bytes_match_the_pinned_hash`,
which fails on any byte change to the vendored file.

**Update both lines together.** Bumping the hash without the comment leaves the
repository with no record of what it is running.

**Those two lines are the whole edit.** The same file mentions `2.0.3` once more,
in its module docstring:

```python
base.html used to pull htmx from `https://unpkg.com/htmx.org@2.0.3` with no
integrity attribute: ...
```

That is a historical record of what #219 fixed, not a pin. **Leave it alone.**
Rewriting it to a newer version would assert that base.html once loaded a version
it never loaded, which destroys the explanation of why these guards exist.

## Choosing the version — do not trust `/releases/latest`

Pick the version before you fetch anything, and pick it by reading tag names.

htmx marks its prereleases with `prerelease: false`, so GitHub treats them as
stable. As of 2026-07-20 the "latest release" API returns a 4.x beta:

```sh
$ curl -sSL https://api.github.com/repos/bigskysoftware/htmx/releases/latest \
    | grep tag_name
  "tag_name": "v4.0.0-beta5",
```

List the tags and choose the newest stable release on the major line verinote is
already on — read that line off the `# Source:` comment quoted above:

```sh
curl -sSL "https://api.github.com/repos/bigskysoftware/htmx/releases?per_page=30" \
  | grep tag_name
```

**Never let `/releases/latest`, or any tool built on it, choose the version for
you** — it will hand you a prerelease, and the steps below will vendor it without
complaint.

## Updating

Replace `<VER>` with the version you chose above (for example `2.0.9`). Run these
from the repository root.

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

On Linux use `sha256sum` in place of `shasum -a 256` — same digest, different
tool name.

If they disagree, **stop and investigate** — do not pick one. Step 1 has already
overwritten the real file, so put the tree back before you walk away:

```sh
git checkout -- verinote/web/static/htmx.min.js
```

(For 2.0.3 the GitHub release asset, unpkg, and jsDelivr are byte-identical.)

**3. Update the pin.** Edit `tests/test_base_template_assets.py`: set
`HTMX_SHA256` to the digest you just verified, and update the version and URL in
the `# Source:` comment above it.

**4. Verify.**

```sh
pytest -q tests/test_base_template_assets.py
pytest -q
ruff check .
```

If the hash test fails, it prints the digest of the file on disk — compare that
with the one you verified in step 2. **If they match**, the vendored file is fine
and the constant you pasted in step 3 is wrong; fix the constant. **If they
differ**, the pin and the bytes came from different fetches, so redo step 1
rather than pasting the reported digest in.

**5. Check it by hand.** The tests prove the bytes are pinned and the file is
served; they do not prove htmx still works. Cut off network access, clear the
browser cache, run `verinote ui`, and accept something in the review queue. If it
misbehaves, revert both halves of the change — `git checkout -- verinote/web/static/htmx.min.js`
and the pin you edited in step 3 — rather than leaving a half-updated tree.

A major-version jump can break htmx attributes that no test covers. Crossing one
is not the asset swap this page describes: read the upstream migration guide and
treat it as a code change reaching every template that carries `hx-` attributes,
with its own issue and review.

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
