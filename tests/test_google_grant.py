# SPDX-License-Identifier: MPL-2.0
"""The stored Google OAuth grant (#476): where it lives and what a broken file does.

The load-side discipline is the load-bearing invariant: a *missing* file is
the normal "not connected" state, but a *corrupt* file is a broken connection,
and the two must not read the same — collapsing them would hide the breakage
and hand the user a "connect" screen for a connection that is actually stuck.
"""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from verinote.config import (
    APP_NAME,
    Config,
    GoogleGrant,
    GoogleOAuthCorruptError,
    app_config_dir,
    clear_google_grant,
    google_oauth_grant_path,
    load_google_grant,
    save_google_grant,
    save_settings,
)

_TOKEN = "1//0e-sample-refresh-token-DEADBEEF"
_TOKEN_B = "1//0e-sample-refresh-token-CAFEBABE"
_EMAIL = "sample.user@example.com"
_EMAIL_B = "other.user@example.com"
_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
_SCOPES_B = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)

_GRANT = GoogleGrant(refresh_token=_TOKEN, email=_EMAIL, scopes=_SCOPES)


@pytest.fixture
def isolated_app_config(isolate_app_environment):
    """These tests write to a real filesystem path. The session-wide conftest
    fixture already gives every test its own home — and therefore its own
    `app_config_dir()` — so this only names the dependency the tests rely on.
    """
    return isolate_app_environment


def _corrupt(text: str) -> None:
    path = google_oauth_grant_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the API: save, load, clear ---


def test_save_load_round_trip_then_clear(isolated_app_config):
    save_google_grant(_GRANT)

    loaded = load_google_grant()
    assert loaded == _GRANT
    # JSON hands `scopes` back as a list; the loader must convert, so pin the
    # exact type — `list(scopes) == _SCOPES` would let an unconverted list
    # through and hide the bug.
    assert type(loaded.scopes) is tuple
    # frozen + all-hashable fields: the instance itself stays hashable
    assert len({loaded, loaded}) == 1

    clear_google_grant()
    assert load_google_grant() is None
    assert not google_oauth_grant_path().exists()


def test_a_missing_file_is_not_an_error(isolated_app_config):
    """The normal state before anyone connects."""
    assert load_google_grant() is None


def test_the_token_does_not_survive_a_repr(isolated_app_config):
    """A stray `logger.exception` or an assertion diff must not print the
    credential — the same discipline `Config.api_key` has."""
    assert _TOKEN not in repr(_GRANT)
    assert _TOKEN not in str(_GRANT)


# --- a corrupt file must not read as "no grant" ---


def test_a_corrupt_file_raises_instead_of_reading_as_absent(isolated_app_config):
    """The core regression: None here would render as a healthy "not
    connected" screen for a broken connection."""
    _corrupt("{not json")
    with pytest.raises(GoogleOAuthCorruptError):
        load_google_grant()


def test_an_undecodable_file_is_an_error_not_an_absent_grant(isolated_app_config):
    """Bytes that are not UTF-8 at all: the file is present, so the answer is
    "broken", never "not connected"."""
    path = google_oauth_grant_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\xff\xfe not utf-8")
    with pytest.raises(GoogleOAuthCorruptError):
        load_google_grant()


@pytest.mark.parametrize(
    ("payload"),
    [
        "[1, 2, 3]",  # not an object
        json.dumps({"version": 2, "refresh_token": "r", "email": "e", "scopes": ["s"]}),
        json.dumps({"version": True, "refresh_token": "r", "email": "e", "scopes": ["s"]}),  # True == 1
        json.dumps({"refresh_token": "r", "email": "e", "scopes": ["s"]}),  # no version
        json.dumps({"version": 1, "refresh_token": "", "email": "e", "scopes": ["s"]}),
        json.dumps({"version": 1, "refresh_token": "   ", "email": "e", "scopes": ["s"]}),
        json.dumps({"version": 1, "refresh_token": 123, "email": "e", "scopes": ["s"]}),
        json.dumps({"version": 1, "email": "e", "scopes": ["s"]}),  # no token
        json.dumps({"version": 1, "refresh_token": "r", "email": "", "scopes": ["s"]}),
        json.dumps({"version": 1, "refresh_token": "r", "scopes": ["s"]}),  # no email
        json.dumps({"version": 1, "refresh_token": "r", "email": "e", "scopes": "s"}),
        json.dumps({"version": 1, "refresh_token": "r", "email": "e", "scopes": []}),
        json.dumps({"version": 1, "refresh_token": "r", "email": "e", "scopes": ["ok", 5]}),
        json.dumps({"version": 1, "refresh_token": "r", "email": "e", "scopes": ["ok", "  "]}),
    ],
)
def test_a_wrong_shape_raises_instead_of_coercing(isolated_app_config, payload):
    """`_read_credentials` refuses rather than coerces; the grant loader must
    too — inventing a token out of a number would be inventing a credential."""
    _corrupt(payload)
    with pytest.raises(GoogleOAuthCorruptError):
        load_google_grant()


# --- save validates; the write side never persists a blank ---


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (GoogleGrant(refresh_token="", email=_EMAIL, scopes=_SCOPES), "refresh_token"),
        (GoogleGrant(refresh_token="   ", email=_EMAIL, scopes=_SCOPES), "refresh_token"),
        (GoogleGrant(refresh_token=_TOKEN, email="", scopes=_SCOPES), "email"),
        (GoogleGrant(refresh_token=_TOKEN, email=_EMAIL, scopes=()), "scopes"),
        (GoogleGrant(refresh_token=_TOKEN, email=_EMAIL, scopes=("s", "")), "scope"),
        (GoogleGrant(refresh_token=_TOKEN, email=_EMAIL, scopes=("s", 5)), "scope"),
    ],
)
def test_a_blank_or_empty_grant_is_refused(isolated_app_config, bad, reason):
    """A blank token persisted today is a guaranteed breakage later; refusing
    at the door is the only place that is still cheap."""
    with pytest.raises(ValueError, match=reason):
        save_google_grant(bad)
    assert not google_oauth_grant_path().exists()


# --- recovery: both paths out of a corrupt file work ---


def test_saving_over_a_corrupt_file_is_the_reconnect_recovery(isolated_app_config):
    """A save replaces the whole one-grant file, so overwriting is recovery,
    not clobber — unlike `save_credential`, whose file holds other providers'
    keys and therefore refuses. Pinned so a future "helpful" pre-read does not
    silently turn reconnect into a dead end."""
    _corrupt("{not json")
    save_google_grant(_GRANT)
    assert load_google_grant() == _GRANT


def test_clearing_a_corrupt_file_removes_it(isolated_app_config):
    """[disconnect] must work for a broken connection too — the user cannot
    be required to read back a file that will not read."""
    _corrupt("{not json")
    clear_google_grant()
    assert not google_oauth_grant_path().exists()
    assert load_google_grant() is None


def test_clearing_without_a_file_is_a_noop(isolated_app_config):
    clear_google_grant()
    assert load_google_grant() is None


# --- the file: where it is, and how it is written ---


def test_saving_a_grant_keeps_it_out_of_the_kb(tmp_path, isolated_app_config):
    """A KB is user data that gets synced and shared; a refresh token is
    standing access to a Google account. Assert over every KB file, and call
    `save_settings` first so a KB with no config.json cannot satisfy a
    config.json-only check without proving anything."""
    save_settings(tmp_path, provider="openai", model="m")
    save_google_grant(_GRANT)
    Config.for_root(tmp_path)

    assert _TOKEN in google_oauth_grant_path().read_text(encoding="utf-8")
    assert (tmp_path / "config.json").exists()
    leaked = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and _TOKEN in path.read_bytes().decode("utf-8", "replace")
    ]
    assert leaked == []


def test_the_config_directory_gets_a_gitignore(isolated_app_config):
    save_google_grant(_GRANT)
    assert (app_config_dir() / ".gitignore").read_text(encoding="utf-8") == "*\n"


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes only")
def test_the_grant_file_is_owner_only(isolated_app_config):
    save_google_grant(_GRANT)
    assert google_oauth_grant_path().stat().st_mode & 0o777 == 0o600


# --- concurrency: two processes, one file, never torn ---


def _concurrent_grant_writer(
    expected_dir: str, refresh_token: str, email: str, scopes: tuple, iterations: int
) -> None:
    """Child body. Spawn-safe: module-level, arguments only, no captured
    state. The first check is the guard — if the child resolved a different
    config dir (environment not inherited), fail loudly instead of writing
    anywhere real."""
    from verinote.config import GoogleGrant, app_config_dir, save_google_grant

    actual = str(app_config_dir())
    if actual != expected_dir:
        raise RuntimeError(f"child resolved {actual}, expected {expected_dir}")
    grant = GoogleGrant(refresh_token=refresh_token, email=email, scopes=tuple(scopes))
    for _ in range(iterations):
        save_google_grant(grant)


def test_concurrent_saves_never_leave_a_torn_file(isolate_app_environment):
    """Two processes saving different grants at once: every reader sees a
    whole grant — either A or B, never a mix, never a torn file. The winner
    is not asserted: order is platform-undefined (the lock is a no-op where
    `fcntl` is absent), so the invariant is integrity, not interleaving."""
    ctx = multiprocessing.get_context("spawn")
    expected_dir = str(app_config_dir())
    writers = (
        (expected_dir, _TOKEN, _EMAIL, _SCOPES, 25),
        (expected_dir, _TOKEN_B, _EMAIL_B, _SCOPES_B, 25),
    )
    procs = [
        ctx.Process(target=_concurrent_grant_writer, args=args) for args in writers
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
    for proc in procs:
        assert proc.exitcode == 0

    path = google_oauth_grant_path()
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))  # must parse as a whole
    assert (data["refresh_token"], data["email"]) in {
        (_TOKEN, _EMAIL),
        (_TOKEN_B, _EMAIL_B),
    }
    # and the child wrote to the sandboxed home, not somewhere real
    assert path.parent == Path(expected_dir)
    assert expected_dir.endswith(APP_NAME)
