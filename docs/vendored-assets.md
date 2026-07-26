# Updating vendored assets

verinote ships one third-party asset in its own tree:
`verinote/web/static/htmx.min.js`. Nothing updates it automatically: vendored
bytes are not represented in a dependency manifest. This page defines the
selection, verification, and review procedure for htmx updates.

## What is vendored, and why

htmx used to load from a CDN `<script>` tag. The review UI renders document text
into the DOM, so third-party code ran in the same page as source material
([#219](https://github.com/semantic-reasoning/verinote/issues/219)). The asset is
now self-hosted: `verinote/web/templates/base.html` loads
`/static/htmx.min.js`, and `tests/test_base_template_assets.py` prevents an
absolute-origin asset URL in that template.

htmx is 0BSD-licensed. Vendoring the minified file has no attribution obligation
beyond its source record.

## Current pin and record

There is no manifest, checksum file, or SRI attribute. The source comment and
`HTMX_SHA256` near the top of `tests/test_base_template_assets.py` are the pin:

```python
# Source: htmx 2.0.9, https://unpkg.com/htmx.org@2.0.9/dist/htmx.min.js (0BSD).
# Not 2.0.10, though that is the newer 2.x: neither v2.0.10 nor v2.0.8 has a GitHub
# Release, so the two-source byte comparison this vendoring procedure requires cannot be
# run against them at all. 2.0.10's release-note items are inert here anyway -- it
# restores TypeScript definitions (we vendor only the minified bytes) and wraps a settle
# lookup in CSS.escape() (ids are `INTEGER PRIMARY KEY`, so those selectors are always
# CSS-safe).
HTMX_SHA256 = "57d9191515339922bd1356d7b2d80b1ee3b29f1b3a2c65a078bb8b2e8fd9ae5f"
```

Issue #321 changes neither that `2.0.9` pin, its bytes, nor its hash. On a
future update, change the source record and hash in the same commit as the
asset. Leave the `2.0.3` URL in the test module docstring alone: it records the
old CDN load, not the active pin.

For every future pin, record adjacent to `HTMX_SHA256`: the `v<VER>` tag, exact
raw GitHub URL, npm package `htmx.org@<VER>`, npm `dist.tarball` URL, and verified
SHA-256. Record the commands' date and the GitHub/npm agreement or any approved
manual fallback in the PR or issue, using only synthetic examples when examples
are needed.

## Choosing a version

Do not use `/releases/latest`: htmx prereleases can be marked as GitHub-stable.
Choose only a final `2.x.y` version. The anchored expression below excludes every
prerelease (`-beta`, `-rc`, and similar) and every non-2.x tag.

Run from the repository root with `curl` and `jq` installed. This follows every
GitHub tags page and parses JSON rather than scraping text:

```sh
set -eu

tags=$(mktemp)
npm_meta=$(mktemp)
trap 'rm -f "$tags" "$npm_meta"' EXIT HUP INT TERM

page=1
while :; do
  json=$(curl --fail --location --silent --show-error \
    "https://api.github.com/repos/bigskysoftware/htmx/tags?per_page=100&page=$page")
  count=$(printf '%s\n' "$json" | jq -er \
    'if type == "array" then length else error("GitHub tags response was not an array") end')
  printf '%s\n' "$json" | jq -r \
    'if type == "array" then .[]?.name else error("GitHub tags response was not an array") end' \
    >> "$tags"
  [ "$count" -lt 100 ] && break
  page=$((page + 1))
done

curl --fail --location --silent --show-error \
  -o "$npm_meta" https://registry.npmjs.org/htmx.org

github_ver=$(jq -Rrse '
  split("\n")
  | map(select(test("^v2\\.[0-9]+\\.[0-9]+$")))
  | map({version: ltrimstr("v"), key: (ltrimstr("v") | split(".") | map(tonumber))})
  | sort_by(.key) | last | .version
  | if . == null then error("no final 2.x GitHub tag") else . end
' "$tags")
npm_ver=$(jq -er '
  [.versions | keys[] | select(test("^2\\.[0-9]+\\.[0-9]+$"))
   | {version: ., key: (split(".") | map(tonumber))}]
  | sort_by(.key) | last | .version
  | if . == null then error("no final 2.x npm version") else . end
' "$npm_meta")

[ "$github_ver" = "$npm_ver" ] || {
  printf 'stop: GitHub tag is %s; npm version is %s\n' "$github_ver" "$npm_ver" >&2
  exit 1
}
printf '%s\n' "$github_ver"
```

Use the printed value as `<VER>`. The newest final 2.x GitHub tag and newest
final 2.x npm version must agree exactly. A mismatch, an API error, malformed
JSON, or no candidate is a stop condition; do not select an older intersection
or substitute a release asset.

If GitHub API pagination is unavailable, manually traverse the repository's
[Tags](https://github.com/bigskysoftware/htmx/tags) pages, identify the newest
`v2.x.y` tag, then run the following with that exact tag. It computes npm's
newest final `2.x.y` version with the same anchored filter and numeric sort as
above, and stops unless it exactly matches the manually identified GitHub tag:

```sh
set -eu

github_tag=v<VER>
github_ver=$(jq -nr --arg tag "$github_tag" '
  $tag
  | if test("^v2\\.[0-9]+\\.[0-9]+$") then ltrimstr("v")
    else error("GitHub tag is not a final 2.x.y tag")
    end
')

npm_meta=$(mktemp)
trap 'rm -f "$npm_meta"' EXIT HUP INT TERM
curl --fail --location --silent --show-error \
  -o "$npm_meta" https://registry.npmjs.org/htmx.org
npm_ver=$(jq -er '
  [.versions | keys[] | select(test("^2\\.[0-9]+\\.[0-9]+$"))
   | {version: ., key: (split(".") | map(tonumber))}]
  | sort_by(.key) | last | .version
  | if . == null then error("no final 2.x npm version") else . end
' "$npm_meta")

[ "$github_ver" = "$npm_ver" ] || {
  printf 'stop: manually identified GitHub tag is %s; newest final npm version is %s\n' \
    "$github_tag" "$npm_ver" >&2
  exit 1
}
printf '%s\n' "$github_ver"
```

Use the printed value as `<VER>`. Record the unavailable endpoint, pages
inspected, selected tag, and npm metadata timestamp. The acquisition check below
still must succeed; manual selection never waives GitHub/npm agreement.

## Acquiring and verifying bytes

Replace `<VER>` with the agreed version. The destination is not touched until the
raw tagged file and npm tarball extraction have the same SHA-256. The staged file
is created in the destination directory, so the final `mv` is an atomic rename.
Any earlier error removes only temporary files and leaves the original asset.

```sh
set -eu

ver=<VER>
target=verinote/web/static/htmx.min.js
target_dir=$(dirname "$target")
work=$(mktemp -d "${TMPDIR:-/tmp}/htmx.XXXXXX")
stage=$(mktemp "$target_dir/.htmx.min.js.XXXXXX")
cleanup() {
  rm -rf "$work"
  rm -f "$stage"
}
trap cleanup EXIT HUP INT TERM

curl --fail --location --silent --show-error -o "$stage" \
  "https://raw.githubusercontent.com/bigskysoftware/htmx/v$ver/dist/htmx.min.js"

curl --fail --location --silent --show-error -o "$work/npm.json" \
  https://registry.npmjs.org/htmx.org
npm_tarball=$(jq -er --arg ver "$ver" '
  .versions[$ver].dist.tarball
  | strings
  | select(startswith("https://"))
' "$work/npm.json")
curl --fail --location --silent --show-error -o "$work/htmx.tgz" "$npm_tarball"
tar -xzf "$work/htmx.tgz" -C "$work"
npm_asset=$work/package/dist/htmx.min.js
[ -f "$npm_asset" ] || { printf 'npm package lacks dist/htmx.min.js\n' >&2; exit 1; }

raw_sha256=$(shasum -a 256 "$stage" | awk '{print $1}')
npm_sha256=$(shasum -a 256 "$npm_asset" | awk '{print $1}')
[ "$raw_sha256" = "$npm_sha256" ] || {
  printf 'stop: raw SHA-256 %s differs from npm SHA-256 %s\n' \
    "$raw_sha256" "$npm_sha256" >&2
  exit 1
}

mv -f "$stage" "$target"
trap - EXIT HUP INT TERM
rm -rf "$work"
printf 'installed htmx %s with SHA-256 %s\n' "$ver" "$raw_sha256"
```

On systems without `shasum`, replace each `shasum -a 256 FILE | awk '{print $1}'`
with `sha256sum FILE | awk '{print $1}'`. Do not copy a digest from an error into
the pin. After the successful rename, update the source/provenance record and
`HTMX_SHA256` with `raw_sha256`, then verify:

```sh
pytest -q tests/test_base_template_assets.py
pytest -q
ruff check .
```

## Compatibility and browser review

Treat a major-version update as a code change with its own issue and migration
review. For any update, review these exact production htmx contracts:

- **Review rows:** `hx-post` to `/facts/<id>/toggle`, `/facts/<id>/accept`,
  `/facts/<id>/reject`, and `/facts/<id>/amend`; `hx-get` to
  `/facts/<id>/edit` and `/facts/<id>/row`; each has
  `hx-target="#fact-<id>"` and `hx-swap="outerHTML"`.
- **Sources poller:** `hx-get="/sources"`, `hx-trigger="every 2s"`,
  `hx-target="#sources-poll"`, `hx-select="#sources-poll"`, and
  `hx-swap="outerHTML"`; its `hx-select-oob` contains the `#analysis-<id>`,
  `#trust-<id>`, `#evidence-<id>`, and `#actions-<id>` cells for every rendered
  source.
- **Questions:** Translate & run and Repair use `hx-post` to
  `/questions/translate` and `/questions/repair`, respectively, with
  `hx-target="body"` and `hx-swap="outerHTML"`; a pending or running repair
  uses `hx-get="/questions"`, `hx-trigger="every 2s"`, `hx-target="body"`,
  and `hx-swap="outerHTML"`.
- **Settings:** Test connection has `hx-post="/settings/test"`,
  `hx-target="main"`, `hx-select="main"`, `hx-swap="outerHTML"`,
  `hx-disabled-elt="find button"`, and `hx-indicator="#connection-testing"`.
- **Headers:** confirm the incoming `HX-Request` branch and every outgoing
  `HX-Redirect` response path in `verinote/web/app.py`; confirm a review action
  that auto-accepts a different fact returns `HX-Refresh: true`.

Use an isolated synthetic KB only. Before browser testing, prepare: two
review-eligible synthetic facts whose decision can auto-accept a separate row; a
synthetic source with a running extraction job so the Sources poller is rendered;
one translatable synthetic question, one `review_required` question, and a
pending or running repair job; and a configured provider that can complete the
Settings connection check. Start `verinote ui`, open browser DevTools with
Network logging and cache disabled, then check these observable results:

- **Review:** toggle, accept, reject, edit, amend, and cancel swap the affected
  row and leave its controls usable. A decision that changes a different row
  returns `HX-Refresh: true` and reloads the page.
- **Sources:** while the synthetic job runs, `/sources` polls every two seconds;
  each response updates `#sources-poll` and its selected analysis, trust,
  evidence, and actions cells out of band. At a terminal job state, the returned
  poller has no `hx-trigger` and further requests stop.
- **Questions:** Translate & run replaces the page with the translated outcome.
  Repair starts and polls a job for the synthetic `review_required` question, and
  the displayed counts and terminal status update without a stale running state.
- **Settings:** Test connection disables its button and shows
  `#connection-testing` while pending; the indicator hides and the button is
  usable after the response.
- **Offline asset:** block all non-loopback network traffic, keep the local UI
  server reachable, clear the browser cache, and reload. `/static/htmx.min.js`
  must load from localhost with no external asset request; repeat a review-row
  action successfully.

## When to check

- Watch [bigskysoftware/htmx](https://github.com/bigskysoftware/htmx) releases.
- Watch the GitHub Advisory Database and run `npm audit` against `htmx.org`.
- Perform this check once a quarter and out of band for a security fix.
