#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# macOS/Linux launcher: provision the virtual environment if it is missing, then
# run verinote. The POSIX counterpart of scripts/run.cmd.
#
#   scripts/run.sh                 -> verinote ui   (opens http://127.0.0.1:8731)
#   scripts/run.sh ui --port 9000  -> verinote ui --port 9000
#   scripts/run.sh status          -> verinote status
#
# Arguments are passed through verbatim. Only the no-argument case injects a
# subcommand, because `--version` and `--help` live on the top-level parser and
# a launcher that silently rewrote them into `verinote ui --version` would turn
# the two flags a new user is likeliest to try into an argparse error.
#
# Environment overrides (all optional):
#   PYTHON             interpreter to build the venv with; wins outright
#   VERINOTE_VENV      venv location            (default: <repo>/.venv)
#   VERINOTE_EXTRAS    pyproject extras         (default: anthropic,ingest)
#   VERINOTE_REINSTALL set to 1 to force the install step to run
#   VERINOTE_DRY_RUN   set to 1 to print the resolution and exit, changing nothing
#
# run.cmd's VERINOTE_NO_PAUSE has no counterpart here, and neither does the
# double-click probe that guards it. Explorer closes a .cmd window the instant
# the script exits, so a failure message would vanish with it; Terminal.app
# defaults to "Close if the shell exited cleanly", so the same message already
# survives a failed run. A `pause` equivalent here would instead hang every
# scripted run that left stdin attached to a terminal.
#
# This script deliberately never sets VERINOTE_ROOT. That variable takes
# precedence over the active KB saved by the browser picker (verinote/cli.py,
# `_config_for`), and the web app refuses to save an active KB while it is set
# — so a root defaulted here would shadow the user's own KB and lock them out
# of correcting it from the UI. An inherited VERINOTE_ROOT is passed through
# untouched. The platform default KB root is already outside this working tree
# (docs/configuration.md), which is where that constraint belongs.
#
# Targets bash 3.2: that is what macOS ships, and it has neither associative
# arrays nor `${x^^}`. `set -e` is deliberately not used — every failure below
# is reported with its own remedy, which a bare exit-on-error would replace with
# silence.

set -u

say() { printf '[verinote] %s\n' "$*"; }

# Diagnostics go to stderr, progress and the dry-run report to stdout, so a
# caller can capture the report without the errors bleeding into it.
fail() {
  for _line in "$@"; do
    printf '[verinote] %s\n' "$_line" >&2
  done
  exit 1
}

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Every path below is derived from `repo`, and the ways it can come out wrong
# are all silent: `dirname` missing from a pinned PATH makes the substitution
# empty, so `cd /..` lands on `/` and succeeds. The install would then run as
# `pip install -e "/[anthropic,ingest]"` and fail talking about a requirement
# the user never typed. Checking for the file that must be there names the real
# problem instead, and guarantees it exists for the hash below.
[ -f "$repo/pyproject.toml" ] || fail \
  "error: could not locate the verinote repository from \"${BASH_SOURCE[0]}\"." \
  "Expected to find pyproject.toml in \"$repo\"." \
  'Run this script from its place in a verinote checkout.'

venv="${VERINOTE_VENV:-$repo/.venv}"
# An interrupted `python -m venv` leaves the directory but no interpreter, so
# the interpreter is the existence test, never the directory.
venvpy="$venv/bin/python"
stamp="$venv/.verinote-install"
extras="${VERINOTE_EXTRAS:-anthropic,ingest}"

# `anthropic` matches the built-in default provider (verinote/config.py), so the
# common path works after one launch rather than two. `ingest` makes docx/pdf
# upload work. The `test` extra from requirements.txt is omitted: a launcher
# provisions the app, not a dev harness. Vendor-neutral users want
# VERINOTE_EXTRAS=ingest and an ollama or claudecli provider; neither adapter
# needs an SDK.
#
# Unlike run.cmd's guard, this one is not about command injection: `$extras` is
# only ever used inside a quoted expansion, and the shell does not re-parse the
# result of one, so the cmd.exe failure mode (a `&` truncating the stamp write
# and silently reinstalling on every launch) cannot occur here. What remains is
# a validity check. Extras are PEP 508 identifiers, so anything outside this set
# is a typo, and refusing it names the mistake instead of letting pip fail with
# a message about a requirement the user never typed.
case "$extras" in
  *[!A-Za-z0-9._,-]*)
    fail 'error: VERINOTE_EXTRAS contains characters that are not allowed.' \
         'Use a comma-separated list of extra names, e.g.' \
         '    VERINOTE_EXTRAS=anthropic,ingest' ;;
esac

dry_run() {
  say 'dry run - nothing was created or installed.'
  printf '  repo        "%s"\n' "$repo"
  printf '  venv        "%s"\n' "$venv"
  printf '  interpreter "%s"\n' "$py"
  printf '  extras      "%s"\n' "$extras"
  exit 0
}

resolve_python() {
  # An explicit PYTHON wins outright and must not fall through to discovery:
  # silently ignoring it would run a different interpreter than the one asked
  # for.
  if [ -n "${PYTHON:-}" ]; then
    py="$PYTHON"
    return 0
  fi
  # Newest CI-tested version first; keep in sync with the matrix in
  # .github/workflows/ci.yml. The bare `python3`/`python` tail is the fallback
  # for the systems that expose no versioned name at all: `python3` is the only
  # spelling on many machines, while activated virtualenvs and older images
  # expose `python` alone, so hard-coding either name breaks half of them.
  for _cand in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$_cand" >/dev/null 2>&1; then
      py="$_cand"
      return 0
    fi
  done
  return 1
}

check_version() {
  _ver="$("$py" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null)" || _ver=''
  # Deliberately unquoted: the two fields are split on whitespace. This runs in
  # a function, so it rewrites the function's positional parameters and leaves
  # the script's own "$@" — the user's verinote arguments — untouched.
  set -- $_ver
  _major="${1:-}"
  _minor="${2:-}"
  # Non-numeric output would make the comparisons below a shell error rather
  # than a diagnosis, and an interpreter that cannot answer is not usable.
  case "${_major}.${_minor}" in
    ''|*[!0-9.]*|.*|*.)
      fail "error: \"$py\" could not report its version, so it is not usable." \
           'Set PYTHON to a working Python 3.11+ interpreter.' ;;
  esac
  if [ "$_major" -lt 3 ] || { [ "$_major" -eq 3 ] && [ "$_minor" -lt 11 ]; }; then
    fail "error: Python $_major.$_minor is too old; verinote requires 3.11 or newer." \
         'Set PYTHON to a newer interpreter, e.g.' \
         '    PYTHON=/opt/homebrew/bin/python3.12 scripts/run.sh'
  fi
  # Above the tested ceiling we warn and continue rather than refuse:
  # pyproject's `requires-python = ">=3.11"` has no upper bound, and a launcher
  # must not enforce a stricter contract than the package it installs. The real
  # failure mode is a dependency with no wheel for this version falling back to
  # a source build, which the warning makes legible in advance.
  if [ "$_major" -eq 3 ] && [ "$_minor" -gt 13 ]; then
    printf '[verinote] %s\n' \
      "Warning: Python $_major.$_minor is newer than the versions this" \
      'project tests (3.11-3.13). If the install fails on a package' \
      'with no matching wheel, set PYTHON to a 3.12 or 3.13 and retry.' >&2
  fi
}

fresh=''
if [ ! -x "$venvpy" ]; then
  resolve_python || fail \
    'error: no usable Python found.' \
    'Install Python 3.11 or newer from https://www.python.org/downloads/' \
    '(or `brew install python@3.12`), or set PYTHON to the interpreter you' \
    'want, e.g.' \
    '    PYTHON=/opt/homebrew/bin/python3.12 scripts/run.sh'
  # Checked before the dry run so that `VERINOTE_DRY_RUN=1` still reports an
  # unusable interpreter rather than printing it as if it would work.
  check_version

  [ "${VERINOTE_DRY_RUN:-}" = '1' ] && dry_run

  say "Creating the virtual environment at \"$venv\"."
  "$py" -m venv "$venv" || fail \
    "error: could not create the virtual environment at \"$venv\"." \
    'Remove that directory and try again, or set VERINOTE_VENV elsewhere.'
  [ -x "$venvpy" ] || fail \
    "error: could not create the virtual environment at \"$venv\"." \
    'Remove that directory and try again, or set VERINOTE_VENV elsewhere.'
  fresh=1
else
  # Steady state runs no interpreter discovery at all: a venv built with 3.12
  # must never silently rebase onto whatever `python3` resolves to today.
  if [ "${VERINOTE_DRY_RUN:-}" = '1' ]; then
    py="$venvpy"
    dry_run
  fi
fi

# Reinstall only when the declared dependencies or the chosen extras actually
# changed. The signature is the content hash of pyproject.toml, not its
# timestamp: two edits inside the same minute share a timestamp, which would
# claim "fresh" when it is not. The hash is taken with the venv interpreter,
# which is guaranteed to exist by this point, rather than with sha256sum or
# shasum -- those are spelled differently on macOS and Linux, and picking one
# would make the launcher's behaviour depend on which coreutils is installed.
pjsig="$("$venvpy" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$repo/pyproject.toml" 2>/dev/null)" || pjsig=''
# Both inputs were checked above -- pyproject.toml exists, and venvpy is
# executable -- so an empty hash here is a real fault (an unreadable file, a
# broken interpreter), not a platform the launcher should route around. Saying
# so beats substituting a placeholder that would compare unequal forever and
# reinstall on every single launch while looking like it worked.
[ -n "$pjsig" ] || fail \
  "error: could not hash \"$repo/pyproject.toml\"." \
  'Check that the file is readable and that the interpreter at' \
  "    \"$venvpy\"" \
  'still runs.'

want="$pjsig~$extras"
have=''
if [ -f "$stamp" ]; then
  read -r have < "$stamp" || have=''
fi

needinstall=''
why='Dependencies changed'
[ "$have" = "$want" ] || needinstall=1
# The console script is what `[project.scripts]` regenerates; its absence is the
# one way this script notices a `pip uninstall` done behind its back.
if [ ! -x "$venv/bin/verinote" ]; then
  needinstall=1
  why='The verinote command is missing from the environment'
fi
if [ "${VERINOTE_REINSTALL:-}" = '1' ]; then
  needinstall=1
  why='VERINOTE_REINSTALL is set'
fi

if [ -n "$needinstall" ]; then
  # Say which of the three reasons actually triggered this, rather than blaming
  # a dependency change for an install the caller forced.
  if [ -n "$fresh" ]; then
    say 'First run: installing verinote and its dependencies.'
  else
    say "$why: reinstalling verinote."
  fi
  say 'This downloads packages and can take several minutes.'
  "$venvpy" -m pip install -e "$repo[$extras]" || fail \
    'error: installing dependencies failed.' \
    'Check the pip output above. To start over, delete' \
    "    \"$venv\"" \
    'and run this script again.'
  printf '%s\n' "$want" > "$stamp"
fi

# `exec` hands the app the terminal and makes its exit code this script's own,
# which is the whole of run.cmd's %ERRORLEVEL% plumbing.
if [ "$#" -eq 0 ]; then
  exec "$venv/bin/verinote" ui
fi
exec "$venv/bin/verinote" "$@"
