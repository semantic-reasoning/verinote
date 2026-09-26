# SPDX-License-Identifier: MPL-2.0
"""The Google Forms sheet client — token refresh and value reads, zero dependencies.

#486 fixes the transport that #478's ``fetch_form_responses`` rides on: the
dependency policy, the timeouts, the quota/retry behaviour, and the test seam.
The API surface is exactly two calls — ``POST https://oauth2.googleapis.com/token``
(refresh_token grant) and
``GET https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range}`` —
so the official SDK's value (retries, auth plumbing) does not buy enough against
its dependency tree for a codebase that keeps its core to five dependencies and
vendors even its HTML assets (``pyproject.toml`` optional-extras policy,
``docs/vendored-assets.md``). This module is the stdlib-``urllib`` path, in the
same family as ``verinote/llm/openrouter_adapter.py`` and ``ollama_adapter.py``:
explicit timeout constants, errors normalised into a small exception hierarchy,
and one injection seam for the test suite.

What this module deliberately is NOT:

* A scheduler. It retries *inside* one ``read_sheet_values`` call only, with a
  bounded attempt count and a bounded total wait. Across-call backoff is #479's
  ``form_sources.next_check_at`` (429/quota is a scheduling state, never a
  ``last_error_kind`` value — that closed set holds user-actionable states only).
  The two owners never compound: a failed call hands back ``retryable=True`` and
  stops.
* A KB writer. It knows nothing about ``last_error_kind`` or watermarks. #479
  maps ``TokenRefreshError`` → ``token_expired`` and ``SheetReadError(403)`` →
  ``sheet_forbidden``; the ``status``/``retryable`` fields here are that input.

The credential contract (#476): the caller passes a ``verinote.config.GoogleGrant``
— the dedicated refresh-token holder — never the whole ``Config``. That is the
``_MODEL_LISTERS`` hazard in ``verinote/web/app.py`` avoided by construction:
there is no settings object in reach for a later edit to hand a credential to
the wrong place. The OAuth ``client_id`` is a separate machine-level identity
(#484 supplies it from machine config, not from the grant), which is why it is
an explicit parameter rather than something read from an environment here.

Rotation is the load-bearing detail. Google rotates refresh tokens: when a
refresh response carries a new one, the old one is dead immediately. So *any*
exit from ``read_sheet_values`` — success, 403, 404, bounded-attempt exhaustion,
the second 401, even the second refresh failing — returns or raises the latest
rotated token it holds (``SheetReadResult.rotated_refresh_token`` or
``GoogleSheetsError.live_refresh_token``, ``None`` when nothing rotated since
the grant the caller passed in). The caller's duty (#478/#479): when that value
is not ``None``, persist it with ``save_google_grant`` before anything else.
Dropping it would turn a perfectly healthy connection into next check's
``invalid_grant`` and force the user to reconnect over a 403 that had nothing
to do with the token.

Single caller: this module takes no locks. Two threads refreshing the same
grant can race a rotation (the loser gets ``invalid_grant``); the #479 worker
serialises checks of the same grant. Do not call ``read_sheet_values``
concurrently for one grant.

Network-free by design: the base suite never dials Google. The one seam is the
module-level ``_open`` (and ``_sleep`` for backoff waits); tests replace those
and nothing else. Standard proxy environment variables are honoured through the
``urllib`` opener, as with the LLM adapters.
"""

from __future__ import annotations

import email.utils
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from verinote.config import GoogleGrant

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"

# The form check runs in the web process's thread (a background *job*, not a
# background *process*), so these bounds are deliberate and named, in the
# ``MODEL_LIST_TIMEOUT_SECONDS`` sense: narrow where the caller waits.
TOKEN_REFRESH_TIMEOUT_SECONDS = 10.0
SHEET_READ_TIMEOUT_SECONDS = 30.0
SHEET_READ_MAX_ATTEMPTS = 3  # bounded; an unbounded retry loop burns the quota
SHEET_READ_BACKOFF_BASE_SECONDS = 2.0  # geometric: 2s, 4s, ...
RETRY_AFTER_MAX_WAIT_SECONDS = 30.0  # a single Retry-After never waits longer
SHEET_READ_MAX_TOTAL_WAIT_SECONDS = 45.0
# The ceiling must sit *below* the largest reachable legitimate wait
# (two 30s-clamped waits = 60s) or it can never fire; 45s makes "stop waiting
# and hand the scheduling to #479" a real, testable outcome instead of a
# constant that is always true.
USER_AGENT = "verinote-form-sync/1 (stdlib urllib)"

_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GoogleSheetsError(Exception):
    """Base error of the sheet client.

    ``status`` is the HTTP status when there was one (``None`` for transport
    failures), and ``retryable`` tells #479 whether the failure is a
    scheduling problem (back off to ``next_check_at``) or a definitive one
    (map it: 401-side → ``token_expired``, 403 → ``sheet_forbidden``).

    ``live_refresh_token`` carries the most recently rotated refresh token, if
    one exists (see the module docstring's rotation contract). It is a plain
    attribute on purpose: it must never appear in the message, in ``args``, or
    in ``repr`` — the same discipline as ``GoogleGrant.refresh_token``
    (``field(repr=False)``), extended to the failure paths.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        live_refresh_token: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.live_refresh_token = live_refresh_token


class TokenRefreshError(GoogleSheetsError):
    """The token exchange failed, or a freshly minted token was rejected twice.

    Definitive by policy: the token POST is never retried. Google may have
    already rotated the refresh token by the time a timed-out response turns
    out to have succeeded, and a second attempt would then fail with
    ``invalid_grant`` — manufacturing the exact breakage the retry was meant
    to prevent.
    """


class SheetReadError(GoogleSheetsError):
    """The sheet value read failed.

    ``status``/``retryable`` are #479's mapping input; the exception name and
    message must stay free of provider detail for the same reason #477 keeps
    ``last_error_kind`` a closed set — the rendered page must not leak an HTTP
    status or an exception name into the user's screen.
    """


class RedirectRefusedError(GoogleSheetsError):
    """A 3xx arrived at a Google endpoint; the request was refused, not followed.

    ``urllib``'s default behaviour on a cross-host 3xx re-issues a POST as a
    GET toward the ``Location`` — for the token endpoint that would ship the
    refresh-token body to a host the caller never named. The handler below
    raises this instead; the call site re-maps it to the endpoint's own error
    type (token path → ``TokenRefreshError``, read path → ``SheetReadError``)
    so a redirect is never misreported as "your token expired".
    """


class _ReadUnauthorized(Exception):
    """A 401 on the value read: the protocol (not the caller) must react."""


class _ReadRetriable(Exception):
    """A 429/5xx/timeout on the value read: the retry loop must react."""

    def __init__(self, status: int | None, retry_after: str | None) -> None:
        super().__init__(status, retry_after)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class TokenBundle:
    """One refresh result. ``new_refresh_token`` is ``None`` unless Google
    rotated — and when it rotated, the token the grant still holds is dead."""

    access_token: str = field(repr=False)
    new_refresh_token: str | None = field(default=None, repr=False)
    expires_in: int = 3600


@dataclass(frozen=True)
class SheetReadResult:
    """The rows of the range, and the rotated refresh token to persist, if any."""

    rows: tuple[tuple[str, ...], ...]
    rotated_refresh_token: str | None = field(default=None, repr=False)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Any 3xx is an error here. See ``RedirectRefusedError``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, msg, headers, newurl
        raise RedirectRefusedError(
            f"the Google endpoint redirected the request (status {code}); "
            "it was refused, not followed",
            status=code,
            retryable=False,
        )


# Both endpoints dial through this opener, so the refusal applies to the token
# POST (secret body) and the value GET alike; a legitimate Google endpoint
# never 3xxs, and honouring proxy environment variables comes with it.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirect())


def _open(req: urllib.request.Request, *, timeout: float):
    """The single transport seam (#486: one seam, not two).

    Tests replace this module attribute; production body is the one line below,
    and the test suite pins that it is THIS opener (the no-redirect one) that
    gets dialled — not the default ``urllib`` opener.
    """
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


_sleep = time.sleep


def _parse_retry_after(value: str | None) -> float | None:
    """A ``Retry-After`` header into a wait in seconds, or ``None`` (→ backoff).

    Accepts RFC 7231 delta-seconds and HTTP-date; anything else falls back to
    the geometric backoff rather than guessing. A past date waits nothing. Both
    forms are clamped to ``RETRY_AFTER_MAX_WAIT_SECONDS`` so a hostile or
    confused header cannot hold the web process's thread for a day.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return float(min(int(text), RETRY_AFTER_MAX_WAIT_SECONDS))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # RFC 7231 HTTP-dates are GMT; parsedate_to_datetime returns them naive.
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return 0.0
    return min(delta, RETRY_AFTER_MAX_WAIT_SECONDS)


def _validate_sheet_id(sheet_id: str) -> None:
    if not isinstance(sheet_id, str) or not _SHEET_ID_RE.fullmatch(sheet_id):
        raise ValueError(
            "sheet_id must be 1-64 characters of [A-Za-z0-9_-] "
            "(a Google Sheet identifier, not a URL)"
        )


def _validate_value_range(value_range: str) -> None:
    if not isinstance(value_range, str) or not value_range.strip():
        raise ValueError("value_range must be a non-blank A1 string")
    if any(ord(ch) < 0x20 or ch == "\\" for ch in value_range):
        raise ValueError(
            "value_range must not contain control characters or backslashes"
        )


def _validate_client_id(client_id: str) -> None:
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-blank string")


def _do_refresh(
    refresh_token: str, client_id: str, scopes: tuple[str, ...]
) -> TokenBundle:
    """One token POST. Never retried — see ``TokenRefreshError``."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _open(req, timeout=TOKEN_REFRESH_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except RedirectRefusedError as exc:
        raise TokenRefreshError(
            f"the token endpoint redirected the request (status {exc.status}); "
            "it was refused, not followed",
            status=exc.status,
            retryable=False,
        ) from exc
    except urllib.error.HTTPError as exc:
        # EVERY non-2xx here is a definitive token failure: 400 invalid_grant,
        # 401, 403, 500, 503 — the error *field* of the body is deliberately
        # not quoted into the message (it is provider detail, and the message
        # must stay secret-free), and none of them is a retry candidate.
        raise TokenRefreshError(
            f"token refresh failed (status {exc.code})",
            status=exc.code,
            retryable=False,
        ) from exc
    except json.JSONDecodeError as exc:
        # A 200 whose body is not JSON: the endpoint answered, so this is a
        # bad answer, not a lost connection — same outcome (definitive, never
        # retried), a distinct label for #479's user-facing mapping.
        raise TokenRefreshError(
            "token refresh response was not valid JSON",
            status=None,
            retryable=False,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - normalise transport errors
        raise TokenRefreshError(
            "token refresh failed (transport error)",
            status=None,
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise TokenRefreshError(
            "token refresh response did not match the expected shape",
            status=None,
            retryable=False,
        )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenRefreshError(
            "token refresh response did not match the expected shape: "
            "no access_token",
            status=None,
            retryable=False,
        )
    expires_in = payload.get("expires_in", 3600)
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or expires_in <= 0
    ):
        raise TokenRefreshError(
            "token refresh response did not match the expected shape: "
            "expires_in",
            status=None,
            retryable=False,
        )
    new_refresh = payload.get("refresh_token")
    if new_refresh is not None and (
        not isinstance(new_refresh, str) or not new_refresh
    ):
        raise TokenRefreshError(
            "token refresh response did not match the expected shape: "
            "refresh_token",
            status=None,
            retryable=False,
        )
    return TokenBundle(
        access_token=access_token,
        new_refresh_token=new_refresh if new_refresh is not None else None,
        expires_in=int(expires_in),
    )


def refresh_access_token(grant: GoogleGrant, client_id: str) -> TokenBundle:
    """Exchange the grant's refresh token for a fresh access token.

    ``client_id`` is the OAuth client identity (#484 owns it, from machine
    config) — the grant holds the refresh token and nothing else, by #476.
    ``TokenBundle.new_refresh_token`` is set only when Google rotated; the
    caller must then persist it (``save_google_grant``) or the next exchange
    will be ``invalid_grant``.
    """
    _validate_client_id(client_id)
    return _do_refresh(grant.refresh_token, client_id, grant.scopes)


def _read_once(
    access_token: str, sheet_id: str, value_range: str
) -> tuple[tuple[str, ...], ...]:
    """One GET of the range. Raises the protocol signals, not caller errors."""
    url = (
        f"{SHEETS_BASE_URL}/{sheet_id}/values/"
        f"{urllib.parse.quote(value_range, safe='')}"
    )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _open(req, timeout=SHEET_READ_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except RedirectRefusedError as exc:
        raise SheetReadError(
            f"the sheet endpoint redirected the request (status {exc.status}); "
            "it was refused, not followed",
            status=exc.status,
            retryable=False,
        ) from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise _ReadUnauthorized() from exc
        if exc.code in (403, 404):
            # Definitive: 403 is #479's sheet_forbidden, 404 is a deleted
            # sheet; neither is cured by waiting.
            raise SheetReadError(
                f"sheet read failed (status {exc.code})",
                status=exc.code,
                retryable=False,
            ) from exc
        if exc.code == 429 or 500 <= exc.code < 600:
            headers = exc.headers
            retry_after = headers.get("Retry-After") if headers else None
            raise _ReadRetriable(exc.code, retry_after) from exc
        raise SheetReadError(
            f"sheet read failed (status {exc.code})",
            status=exc.code,
            retryable=False,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - transport: timeout, reset, ...
        raise _ReadRetriable(None, None) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
        # "values" absent means an empty range; a form sheet always has its
        # header row, so absence is a shape failure, not "no responses".
        raise SheetReadError(
            "the sheet read response did not include a values array",
            status=None,
            retryable=False,
        )
    rows: list[tuple[str, ...]] = []
    for row in payload["values"]:
        if not isinstance(row, list):
            raise SheetReadError(
                "the sheet read response did not match the expected shape: "
                "a row is not a list",
                status=None,
                retryable=False,
            )
        for cell in row:
            if not isinstance(cell, str):
                raise SheetReadError(
                    "the sheet read response did not match the expected shape: "
                    "a cell is not a string",
                    status=None,
                    retryable=False,
                )
        rows.append(tuple(row))
    return tuple(rows)


def _live_or_none(live: str, grant: GoogleGrant) -> str | None:
    """The rotated token to hand back, or ``None`` when nothing rotated.

    Returning the unchanged grant token would make the caller re-save the
    exact same credential — harmless but noise; ``None`` says "nothing new".
    """
    return live if live != grant.refresh_token else None


def read_sheet_values(
    grant: GoogleGrant,
    client_id: str,
    sheet_id: str,
    value_range: str,
) -> SheetReadResult:
    """Read one A1 range of a sheet, with the 401 → refresh → one-retry protocol.

    The protocol (``live`` tracks the live refresh token as rotations happen):

    1. refresh with the grant's token; on failure the error is definitive and
       carries ``live_refresh_token=None`` (nothing rotated yet).
    2. read with the access token.
    3. 401 → refresh again **with the live token** (if step 1 rotated, the
       original is already dead) → retry the read. One such recovery per call,
       wherever the 401 arrives (even after a 429 backoff); if the recovery
       refresh itself fails, it is re-raised carrying the live token — dropping
       it here would force a reconnect over a failure the token never caused.
    4. A second 401 is a definitive token failure (``TokenRefreshError``),
       carrying the live token; there is no loop, under any interleaving.
    5. 403/404 are definitive ``SheetReadError`` (403 is #479's
       ``sheet_forbidden``); 429/5xx/timeout retry within the attempt and wait
       budgets, after which the failure is ``retryable=True`` so #479 can
       schedule the next try via ``next_check_at``.
    6. Every exit returns or raises ``_live_or_none(live, grant)`` — the
       rotation contract in the module docstring.
    """
    _validate_sheet_id(sheet_id)
    _validate_value_range(value_range)
    _validate_client_id(client_id)

    live = grant.refresh_token
    bundle = _do_refresh(live, client_id, grant.scopes)
    if bundle.new_refresh_token is not None:
        live = bundle.new_refresh_token

    attempts = 0
    waited = 0.0
    unauthorized_seen = False
    while True:
        attempts += 1
        try:
            rows = _read_once(bundle.access_token, sheet_id, value_range)
        except _ReadUnauthorized as exc:
            # Bounded by the flag, not the attempt index: a 401 that arrives
            # after a 429 backoff is still the first 401 of this call and
            # earns the one refresh-and-retry; a second 401 anywhere is the
            # definitive token failure (no loop, whatever the interleaving).
            if unauthorized_seen:
                raise TokenRefreshError(
                    "the refreshed token was rejected twice; the Google "
                    "connection needs a fresh grant",
                    retryable=False,
                    live_refresh_token=_live_or_none(live, grant),
                ) from exc
            unauthorized_seen = True
            try:
                bundle = _do_refresh(live, client_id, grant.scopes)
            except TokenRefreshError as exc2:
                raise TokenRefreshError(
                    str(exc2),
                    status=exc2.status,
                    retryable=False,
                    live_refresh_token=_live_or_none(live, grant),
                ) from exc2
            if bundle.new_refresh_token is not None:
                live = bundle.new_refresh_token
            continue
        except SheetReadError as exc:
            # Definitive read failures (403/404/shape/redirect): same error,
            # re-raised with the live token attached — the read path's own
            # mapping, not a token failure.
            raise SheetReadError(
                str(exc),
                status=exc.status,
                retryable=False,
                live_refresh_token=_live_or_none(live, grant),
            ) from exc
        except _ReadRetriable as exc:
            if attempts >= SHEET_READ_MAX_ATTEMPTS:
                raise SheetReadError(
                    "the sheet read was still failing after the bounded attempts",
                    status=exc.status,
                    retryable=True,
                    live_refresh_token=_live_or_none(live, grant),
                ) from exc
            retry_after = _parse_retry_after(exc.retry_after)
            wait = (
                retry_after
                if retry_after is not None
                else SHEET_READ_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
            )
            if waited + wait > SHEET_READ_MAX_TOTAL_WAIT_SECONDS:
                # Stop waiting and hand the scheduling to #479's
                # next_check_at; the ceiling is below the largest reachable
                # legitimate wait so this branch is real, not decoration.
                raise SheetReadError(
                    "the sheet read backoff exceeded the total wait ceiling",
                    status=exc.status,
                    retryable=True,
                    live_refresh_token=_live_or_none(live, grant),
                ) from exc
            _sleep(wait)
            waited += wait
            continue
        return SheetReadResult(
            rows=rows,
            rotated_refresh_token=_live_or_none(live, grant),
        )

