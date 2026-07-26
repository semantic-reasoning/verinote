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

`verinote/web/static/htmx.min.js.metadata.json` is the sole active provenance
record for the vendored asset. It is public at
`/static/htmx.min.js.metadata.json` and records the version, SHA-256, Git tag and
commit, raw GitHub URL, npm package, and npm tarball URL. The asset test strictly
validates this JSON record, its version/tag/URL relationships, its digest, and
its static JSON response.

A pin update must change the metadata and asset together. Leave the `2.0.3` URL
in the test module docstring alone: it records the old CDN load, not the active
pin. Record the commands' date and the GitHub/npm agreement or any approved
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

Replace `<VER>` with the agreed version. The destinations are not touched until
the raw tagged file and npm tarball extraction have the same SHA-256. Each staged
file is created in the destination directory, so each individual `mv` is atomic,
but two renames are not an atomic pair. Immediately before installation, the
script copies both current files to backups. If either installation is incomplete,
its exit handler restores both originals from those backups.

```sh
set -eu

ver=<VER>
target=verinote/web/static/htmx.min.js
metadata="$target.metadata.json"
target_dir=$(dirname "$target")
work=$(mktemp -d "${TMPDIR:-/tmp}/htmx.XXXXXX")
stage_asset=
stage_metadata=
backup_asset=
backup_metadata=
install_started=false
installed=false
cleanup() {
  status=$?
  restore_failed=false
  trap - EXIT HUP INT TERM

  if [ "$install_started" = true ] && [ "$installed" != true ]; then
    printf 'installation did not complete; restoring original asset and metadata\n' >&2
    if ! cp -p "$backup_asset" "$target"; then
      restore_failed=true
    fi
    if ! cp -p "$backup_metadata" "$metadata"; then
      restore_failed=true
    fi
    if [ "$restore_failed" = true ]; then
      printf 'automatic restore failed; backups were retained. Recover with:\n' >&2
      cat >&2 <<EOF
cp -p "$backup_asset" "$target"
cp -p "$backup_metadata" "$metadata"
EOF
      status=1
    fi
  fi

  rm -rf "$work"
  [ -z "$stage_asset" ] || rm -f "$stage_asset"
  [ -z "$stage_metadata" ] || rm -f "$stage_metadata"
  if [ "$restore_failed" != true ]; then
    [ -z "$backup_asset" ] || rm -f "$backup_asset"
    [ -z "$backup_metadata" ] || rm -f "$backup_metadata"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stage_asset=$(mktemp "$target_dir/.htmx.min.js.XXXXXX")
stage_metadata=$(mktemp "$target_dir/.htmx.min.js.metadata.json.XXXXXX")

tag="v$ver"
raw_url="https://raw.githubusercontent.com/bigskysoftware/htmx/$tag/dist/htmx.min.js"
curl --fail --location --silent --show-error -o "$stage_asset" "$raw_url"

tag_commit=$(curl --fail --location --silent --show-error \
  "https://api.github.com/repos/bigskysoftware/htmx/git/ref/tags/$tag" \
  | jq -er '.object | select(.type == "commit") | .sha
      | select(test("^[0-9a-f]{40}$"))')

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

raw_sha256=$(shasum -a 256 "$stage_asset" | awk '{print $1}')
npm_sha256=$(shasum -a 256 "$npm_asset" | awk '{print $1}')
[ "$raw_sha256" = "$npm_sha256" ] || {
  printf 'stop: raw SHA-256 %s differs from npm SHA-256 %s\n' \
    "$raw_sha256" "$npm_sha256" >&2
  exit 1
}

jq -n \
  --arg version "$ver" \
  --arg sha256 "$raw_sha256" \
  --arg tag "$tag" \
  --arg tag_commit "$tag_commit" \
  --arg raw_url "$raw_url" \
  --arg npm_package "htmx.org@$ver" \
  --arg npm_tarball_url "$npm_tarball" \
  '{version: $version, sha256: $sha256, tag: $tag, tag_commit: $tag_commit,
    raw_url: $raw_url, npm_package: $npm_package, npm_tarball_url: $npm_tarball_url}' \
  > "$stage_metadata"

[ -f "$target" ] && [ -f "$metadata" ] || {
  printf 'existing asset or metadata is missing; refusing to install\n' >&2
  exit 1
}
backup_asset=$(mktemp "$target_dir/.htmx.min.js.backup.XXXXXX")
backup_metadata=$(mktemp "$target_dir/.htmx.min.js.metadata.json.backup.XXXXXX")
cp -p "$target" "$backup_asset"
cp -p "$metadata" "$backup_metadata"

# These are separate renames. From this point, cleanup restores both originals
# unless both moves have completed.
install_started=true
mv -f "$stage_asset" "$target"
mv -f "$stage_metadata" "$metadata"
installed=true
printf 'installed htmx %s with SHA-256 %s and updated metadata\n' "$ver" "$raw_sha256"
```

On systems without `shasum`, replace each `shasum -a 256 FILE | awk '{print $1}'`
with `sha256sum FILE | awk '{print $1}'`. Do not copy a digest from an error into
the metadata. A failed `mv`, `HUP`, `INT`, or `TERM` exits through `cleanup` and
restores both originals while installation is incomplete. `SIGKILL` and power
loss cannot run that handler. If restoration itself fails, the handler retains
the backup paths and prints the recovery command. After the successful renames,
verify:

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
