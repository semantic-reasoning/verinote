# SPDX-License-Identifier: MPL-2.0
"""The Google sheet client (#486) against a scripted transport.

Everything runs network-free through the module's single seam (``_open``), in
the ``test_openrouter_adapter`` idiom: the fake records each request (url,
method, headers, body, timeout) and the test asserts on what the production
code actually dialed. Tokens are synthetic samples; the secret-hygiene test
pins that none of them can reach an exception's message, args, or repr.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.parse
from typing import get_type_hints

import pytest

from verinote.config import GoogleGrant
import verinote.google_sheets as gs

REFRESH = "1//0e-SampleRefreshTokenAaBbCcDd=="
ACCESS = "ya29.SampleAccessTokenAbCdEf"
ACCESS2 = "ya29.SampleAccessTokenSecondGh"
ROTATED = "1//0e-SampleRotatedTokenXxYyZz=="
ROTATED2 = "1//0e-SampleRotatedTokenSecondKk=="
CLIENT_ID = "sample-client-id.apps.googleusercontent.com"
SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
SHEET_ID = "1SampleSheetIdAbCdEf_01"

GRANT = GoogleGrant(
    refresh_token=REFRESH,
    email="sample.user@example.com",
    scopes=(SCOPE,),
)


class _Resp:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


class _RawResp:
    """A response whose body is fixed raw bytes (not a JSON-serialised payload)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "_RawResp":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def _http(code: int, *, hdrs: dict | None = None, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(gs.TOKEN_ENDPOINT, code, "error", hdrs, io.BytesIO(body))


def _install(monkeypatch, script: list):
    """Script the transport: one response-or-exception per dial.

    Returns ``(calls, sleeps)`` — the recorded requests and the backoff waits
    the production code asked for.
    """
    calls: list[dict] = []
    sleeps: list[float] = []

    def fake_open(req, *, timeout):
        calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": req.data,
                "timeout": timeout,
            }
        )
        item = script[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("verinote.google_sheets._open", fake_open)
    monkeypatch.setattr("verinote.google_sheets._sleep", fake_sleep)
    return calls, sleeps


def _refresh_payload(access: str = ACCESS, *, rotate: bool = False) -> dict:
    payload = {"access_token": access, "expires_in": 3600}
    if rotate:
        payload["refresh_token"] = ROTATED
    return payload


def _body(call: dict) -> dict:
    text = call["body"].decode("utf-8") if isinstance(call["body"], (bytes, bytearray)) else call["body"]
    return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}


def _no_secret(exc: BaseException) -> None:
    for token in (REFRESH, ACCESS, ACCESS2, ROTATED, ROTATED2):
        for surface in (str(exc), repr(exc), *(str(a) for a in exc.args)):
            assert token not in surface, (token, surface)


# --- refresh_access_token ----------------------------------------------------


def test_refresh_posts_the_grant_fields_with_no_authorization_header(monkeypatch):
    """The refresh_token grant is a form POST to the token endpoint.

    The secret travels in the body (and nowhere else): no Authorization
    header can be built for it, and the timeout is the named constant, not an
    implicit default that would hold the web thread indefinitely.
    """
    calls, _ = _install(monkeypatch, [_Resp(_refresh_payload())])

    bundle = gs.refresh_access_token(GRANT, CLIENT_ID)

    (call,) = calls
    assert call["url"] == gs.TOKEN_ENDPOINT
    assert call["method"] == "POST"
    assert call["timeout"] == gs.TOKEN_REFRESH_TIMEOUT_SECONDS
    body = _body(call)
    assert body == {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": REFRESH,
        "scope": SCOPE,
    }
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in call["headers"]
    assert call["headers"]["user-agent"] == gs.USER_AGENT
    assert bundle.access_token == ACCESS
    assert bundle.new_refresh_token is None
    assert bundle.expires_in == 3600


def test_refresh_reports_a_rotation_when_google_sends_one(monkeypatch):
    """A rotated refresh token is only in the response when it rotated — and
    when it rotated, the old one is dead. Dropping ``new_refresh_token`` here
    is the #476/#478 contract break the rotation design exists to prevent."""
    calls, _ = _install(monkeypatch, [_Resp(_refresh_payload(rotate=True))])

    bundle = gs.refresh_access_token(GRANT, CLIENT_ID)

    assert bundle.new_refresh_token == ROTATED
    assert calls[0]["body"] is not None  # the grant still went out


def test_refresh_400_invalid_grant_is_definitive_and_never_retried(monkeypatch):
    """A rejected grant is not a transport blip: one attempt, definitive error,
    and the provider's error word does not leak into the message."""
    calls, _ = _install(
        monkeypatch, [_http(400, body=b'{"error": "invalid_grant"}')]
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)

    assert err.value.status == 400
    assert err.value.retryable is False
    assert "invalid_grant" not in str(err.value)
    _no_secret(err.value)
    assert len(calls) == 1


def test_refresh_transport_failure_is_definitive_and_never_retried(monkeypatch):
    """After a timeout the server may have rotated the token already, so a
    second POST would manufacture the very invalid_grant it fears: one try."""
    calls, _ = _install(monkeypatch, [urllib.error.URLError("timed out")])

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)

    assert err.value.status is None
    assert err.value.retryable is False
    _no_secret(err.value)
    assert len(calls) == 1


def test_token_endpoint_invalid_json_is_distinct_from_transport(monkeypatch):
    """A 200 whose body is not JSON is a bad answer, not a lost connection:
    same outcome (definitive, never retried), a label #479 can map apart from
    transport failures."""
    calls, _ = _install(monkeypatch, [_RawResp(b"<html>not json here</html>")])

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)

    assert "not valid JSON" in str(err.value)
    assert "transport error" not in str(err.value)
    assert err.value.status is None
    assert err.value.retryable is False
    _no_secret(err.value)
    assert len(calls) == 1


def test_token_endpoint_500_is_definitive(monkeypatch):
    calls, _ = _install(monkeypatch, [_http(500)])
    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)
    assert (err.value.status, err.value.retryable) == (500, False)
    assert len(calls) == 1


def test_token_endpoint_403_is_definitive(monkeypatch):
    calls, _ = _install(monkeypatch, [_http(403)])
    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)
    assert (err.value.status, err.value.retryable) == (403, False)
    assert len(calls) == 1


def test_token_endpoint_redirect_is_refused_not_followed(monkeypatch):
    """A 3xx on the token POST must become a token error at the call site —
    and the handler that raises it must be the one in the production opener."""
    opener = gs._NO_REDIRECT_OPENER
    spy_dials: list[str] = []

    def spy_open(req, timeout=None):
        spy_dials.append(req.get_method())
        raise gs.RedirectRefusedError(
            "the Google endpoint redirected the request (status 302); "
            "it was refused, not followed",
            status=302,
        )

    monkeypatch.setattr(opener, "open", spy_open)

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.refresh_access_token(GRANT, CLIENT_ID)

    assert spy_dials == ["POST"]  # the production _open dialed THIS opener
    assert any(isinstance(h, gs._RefuseRedirect) for h in opener.handlers)
    assert err.value.status == 302
    _no_secret(err.value)


def test_redirect_handler_refuses_a_3xx():
    handler = gs._RefuseRedirect()
    with pytest.raises(gs.RedirectRefusedError) as err:
        handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/")
    assert err.value.status == 302
    _no_secret(err.value)


# --- read_sheet_values: happy path & input validation -------------------------


def test_read_success_url_headers_timeouts_and_rows(monkeypatch):
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            _Resp({"range": "A2:Z", "values": [["Q1", "A1"], ["Q2", "A2"]]}),
        ],
    )

    result = gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert result.rows == (("Q1", "A1"), ("Q2", "A2"))
    assert result.rotated_refresh_token is None
    refresh_call, read_call = calls
    assert refresh_call["url"] == gs.TOKEN_ENDPOINT
    assert refresh_call["timeout"] == gs.TOKEN_REFRESH_TIMEOUT_SECONDS
    assert read_call["url"] == f"{gs.SHEETS_BASE_URL}/{SHEET_ID}/values/A2%3AZ"
    assert read_call["method"] == "GET"
    assert read_call["timeout"] == gs.SHEET_READ_TIMEOUT_SECONDS
    assert read_call["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert read_call["headers"]["accept"] == "application/json"
    assert read_call["headers"]["user-agent"] == gs.USER_AGENT


def test_value_range_is_percent_encoded(monkeypatch):
    """Sheet names hold spaces, quotes, and the ! separator; the range is a URL
    path segment, so it is quoted for the path, not parsed by this module."""
    calls, _ = _install(
        monkeypatch, [_Resp(_refresh_payload()), _Resp({"values": [["ok"]]})]
    )

    gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "'Form Responses 1'!A2:Z")

    assert calls[1]["url"].endswith("/values/%27Form%20Responses%201%27%21A2%3AZ")


def test_invalid_sheet_ids_are_rejected_before_any_network(monkeypatch):
    """A sheet_id is a path segment; slashes, schemes, and control characters
    would rewrite the request's destination, so they are refused at validation."""
    calls, _ = _install(monkeypatch, [])
    for bad in ("abc/def", "http://x", "ab\ncd", "", "a" * 65, "id with space"):
        with pytest.raises(ValueError):
            gs.read_sheet_values(GRANT, CLIENT_ID, bad, "A2:Z")
    assert calls == []


def test_read_sheet_values_takes_the_grant_not_the_config():
    """Signature pin: the credential in reach is the dedicated GoogleGrant,
    never a settings object a later edit could hand to the wrong place."""
    hints = get_type_hints(gs.read_sheet_values)
    assert set(hints) >= {"grant", "client_id", "sheet_id", "value_range", "return"}
    assert hints["grant"] is GoogleGrant
    assert hints["return"] is gs.SheetReadResult


# --- read_sheet_values: the 401 protocol --------------------------------------


def test_401_refreshes_with_the_live_token_and_retries_once(monkeypatch):
    """The second refresh must use the rotated token (the original is dead),
    and the retry is exactly one read."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS, rotate=True)),
            _http(401),
            _Resp(_refresh_payload(ACCESS2)),
            _Resp({"values": [["Q", "A"]]}),
        ],
    )

    result = gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert result.rows == (("Q", "A"),)
    assert result.rotated_refresh_token == ROTATED
    assert [c["url"] for c in calls] == [
        gs.TOKEN_ENDPOINT,
        f"{gs.SHEETS_BASE_URL}/{SHEET_ID}/values/A2%3AZ",
        gs.TOKEN_ENDPOINT,
        f"{gs.SHEETS_BASE_URL}/{SHEET_ID}/values/A2%3AZ",
    ]
    assert _body(calls[2])["refresh_token"] == ROTATED  # live, not the original
    assert calls[3]["headers"]["authorization"] == f"Bearer {ACCESS2}"


def test_double_rotation_hands_back_the_latest_token(monkeypatch):
    """Both refreshes rotate: the second rotation (R2) supersedes the first
    (R1) and is the credential the caller must persist — R1 is already dead."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS, rotate=True)),
            _http(401),
            _Resp({"access_token": ACCESS2, "expires_in": 3600, "refresh_token": ROTATED2}),
            _Resp({"values": [["Q", "A"]]}),
        ],
    )

    result = gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert result.rows == (("Q", "A"),)
    assert result.rotated_refresh_token == ROTATED2  # latest, not R1
    assert _body(calls[2])["refresh_token"] == ROTATED  # repair used R1 (live then)
    assert calls[3]["headers"]["authorization"] == f"Bearer {ACCESS2}"


def test_second_401_is_definitive_and_carries_the_live_token(monkeypatch):
    """No loop: after one refresh-and-retry, a second 401 is a token failure —
    and the token rotated in the first refresh is still handed back."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS, rotate=True)),
            _http(401),
            _Resp(_refresh_payload(ACCESS2)),
            _http(401),
        ],
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is False
    assert err.value.live_refresh_token == ROTATED
    _no_secret(err.value)
    assert len(calls) == 4  # no fifth dial: the loop is closed


def test_second_refresh_failure_carries_the_first_rotation(monkeypatch):
    """The step-4 subpath: the second refresh itself fails. The rotation the
    first refresh produced is the live credential and must still be returned —
    dropping it would force a reconnect over a failure the token never caused."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS, rotate=True)),
            _http(401),
            urllib.error.URLError("timed out"),
        ],
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.live_refresh_token == ROTATED
    _no_secret(err.value)
    assert len(calls) == 3


def test_second_refresh_failure_without_rotation_carries_none(monkeypatch):
    """Guard: when nothing rotated, the error carries None — the caller must
    not be told to re-save the very token it already holds."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS)),
            _http(401),
            urllib.error.URLError("timed out"),
        ],
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.live_refresh_token is None
    assert len(calls) == 3


def test_403_after_rotation_is_definitive_and_carries_the_live_token(monkeypatch):
    """A forbidden sheet is #479's sheet_forbidden — but the connection is
    healthy, so the rotated token must survive the failure for the caller."""
    calls, _ = _install(
        monkeypatch, [_Resp(_refresh_payload(ACCESS, rotate=True)), _http(403)]
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert (err.value.status, err.value.retryable) == (403, False)
    assert err.value.live_refresh_token == ROTATED
    _no_secret(err.value)
    assert len(calls) == 2  # no read retry: 403 is not cured by waiting


def test_404_after_rotation_is_definitive_and_carries_the_live_token(monkeypatch):
    """A deleted sheet (404) is a scheduling/user problem, not a credential
    problem — the rotated token must survive the failure, symmetric with 403."""
    calls, _ = _install(
        monkeypatch, [_Resp(_refresh_payload(ACCESS, rotate=True)), _http(404)]
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert (err.value.status, err.value.retryable) == (404, False)
    assert err.value.live_refresh_token == ROTATED
    _no_secret(err.value)
    assert len(calls) == 2  # no read retry: 404 is not cured by waiting


def test_404_without_rotation_carries_none(monkeypatch):
    calls, _ = _install(monkeypatch, [_Resp(_refresh_payload()), _http(404)])

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert (err.value.status, err.value.retryable) == (404, False)
    assert err.value.live_refresh_token is None
    assert len(calls) == 2


def test_read_path_redirect_is_a_sheet_read_error_not_a_token_error(monkeypatch):
    """The same refusal must not be misreported as token_expired on the read
    path — the cause was the endpoint, not the credential."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            gs.RedirectRefusedError(
                "the sheet endpoint redirected the request (status 302); "
                "it was refused, not followed",
                status=302,
            ),
        ],
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert type(err.value) is gs.SheetReadError
    assert (err.value.status, err.value.retryable) == (302, False)
    assert len(calls) == 2


# --- read_sheet_values: quota / retry budget ----------------------------------


def test_retry_after_delta_seconds_is_honoured(monkeypatch):
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            _http(429, hdrs={"Retry-After": "1"}),
            _Resp({"values": [["ok"]]}),
        ],
    )

    gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert sleeps == [1.0]
    assert len(calls) == 3


def test_retry_after_is_clamped_to_the_single_wait_ceiling(monkeypatch):
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            _http(429, hdrs={"Retry-After": "99999"}),
            _Resp({"values": [["ok"]]}),
        ],
    )

    gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert sleeps == [gs.RETRY_AFTER_MAX_WAIT_SECONDS]


def _http_date(seconds_offset: int) -> str:
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=seconds_offset)
    return when.strftime("%a, %d %b %Y %H:%M:%S GMT")  # RFC 7231, zone-less


def test_retry_after_http_date_past_future_and_garbage(monkeypatch):
    """A past date waits nothing; a far future date hits the clamp; garbage
    falls back to the geometric backoff instead of guessing."""
    for header, expected_wait in (
        (_http_date(-3600), 0.0),
        (_http_date(7200), gs.RETRY_AFTER_MAX_WAIT_SECONDS),
        ("later", gs.SHEET_READ_BACKOFF_BASE_SECONDS),
    ):
        _, sleeps = _install(
            monkeypatch,
            [
                _Resp(_refresh_payload()),
                _http(429, hdrs={"Retry-After": header}),
                _Resp({"values": [["ok"]]}),
            ],
        )
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")
        assert sleeps == [expected_wait], header


def test_5xx_retried_with_geometric_backoff_then_success(monkeypatch):
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            _http(500),
            _http(502),
            _Resp({"values": [["ok"]]}),
        ],
    )

    result = gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert result.rows == (("ok",),)
    assert sleeps == [2.0, 4.0]
    assert len(calls) == 4


def test_401_after_a_quota_backoff_still_earns_the_repair(monkeypatch):
    """The 401 is the first of this call even though a 429 came first: it gets
    the one refresh-and-retry, and the repair's rotation is still handed back."""
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS)),
            _http(429, hdrs={"Retry-After": "1"}),
            _http(401),
            _Resp(_refresh_payload(ACCESS2, rotate=True)),
            _Resp({"values": [["Q", "A"]]}),
        ],
    )

    result = gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert result.rows == (("Q", "A"),)
    assert result.rotated_refresh_token == ROTATED
    assert sleeps == [1.0]
    assert _body(calls[3])["refresh_token"] == REFRESH  # live at that moment
    assert calls[4]["headers"]["authorization"] == f"Bearer {ACCESS2}"


def test_second_401_after_a_quota_backoff_is_still_definitive(monkeypatch):
    """...but only one repair per call: the next 401 ends the call, no loop."""
    calls, _ = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS)),
            _http(429, hdrs={"Retry-After": "1"}),
            _http(401),
            _Resp(_refresh_payload(ACCESS2)),
            _http(401),
        ],
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is False
    assert err.value.live_refresh_token is None
    assert len(calls) == 5


def test_second_401_after_a_repair_and_another_backoff_is_still_definitive(monkeypatch):
    """The repair was already spent on the first 401; a later 429 backoff does
    not buy a second one: the next 401 ends the call, and the repair's
    rotation still survives to be handed back."""
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(ACCESS)),
            _http(401),
            _Resp(_refresh_payload(ACCESS2, rotate=True)),
            _http(429, hdrs={"Retry-After": "1"}),
            _http(401),
        ],
    )

    with pytest.raises(gs.TokenRefreshError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is False
    assert err.value.live_refresh_token == ROTATED
    assert sleeps == [1.0]
    assert len(calls) == 5  # no sixth dial: the loop is closed


def test_5xx_exhausts_attempts_and_stays_retryable(monkeypatch):
    """Exhaustion is a scheduling state for #479, not a user-actionable one:
    retryable=True, and the live token (if rotated) still comes back."""
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload(rotate=True)),
            _http(500),
            _http(500),
            _http(500),
        ],
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is True
    assert err.value.live_refresh_token == ROTATED
    _no_secret(err.value)
    assert sleeps == [2.0, 4.0]
    read_attempts = sum(1 for c in calls if c["url"].startswith(gs.SHEETS_BASE_URL))
    assert read_attempts == gs.SHEET_READ_MAX_ATTEMPTS


def test_total_wait_ceiling_stops_the_retry_loop(monkeypatch):
    """The ceiling (45s) sits below the largest reachable legitimate wait
    (two 30s-clamped waits = 60s), so this is a real branch: the loop stops
    waiting and hands the scheduling to #479 instead of holding the thread."""
    calls, sleeps = _install(
        monkeypatch,
        [
            _Resp(_refresh_payload()),
            _http(429, hdrs={"Retry-After": "9999"}),
            _http(429, hdrs={"Retry-After": "9999"}),
        ],
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is True
    assert sleeps == [gs.RETRY_AFTER_MAX_WAIT_SECONDS]  # one wait, then stop
    assert sum(sleeps) <= gs.SHEET_READ_MAX_TOTAL_WAIT_SECONDS
    read_attempts = sum(1 for c in calls if c["url"].startswith(gs.SHEETS_BASE_URL))
    assert read_attempts == 2


def test_missing_values_array_is_a_definitive_shape_error(monkeypatch):
    """A 200 without values is a shape failure, not 'no responses' — retrying
    a protocol anomaly only burns quota."""
    calls, _ = _install(
        monkeypatch, [_Resp(_refresh_payload()), _Resp({"range": "A2:Z"})]
    )

    with pytest.raises(gs.SheetReadError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    assert err.value.retryable is False
    _no_secret(err.value)
    assert len(calls) == 2


# --- secret hygiene across the failure surface ---------------------------------


@pytest.mark.parametrize(
    "script",
    [
        [_http(400, body=b'{"error": "invalid_grant"}')],
        [urllib.error.URLError("timed out")],
        [_Resp(_refresh_payload(rotate=True)), _http(401), _Resp(_refresh_payload(ACCESS2)), _http(401)],
        [_Resp(_refresh_payload(rotate=True)), _http(403)],
        [_Resp(_refresh_payload(rotate=True)), _http(500), _http(500), _http(500)],
        [_Resp(_refresh_payload()), _Resp({"range": "A2:Z"})],
    ],
    ids=["refresh-400", "refresh-timeout", "double-401", "read-403", "read-5xx", "shape"],
)
def test_secrets_never_reach_the_exception_surfaces(monkeypatch, script):
    """The tokens may appear in the wire body (that is their job), but in no
    exception message, arg, or repr — the #476 repr discipline, extended to
    the failure paths where a rotated token is actually carried."""
    _install(monkeypatch, script)

    with pytest.raises(gs.GoogleSheetsError) as err:
        gs.read_sheet_values(GRANT, CLIENT_ID, SHEET_ID, "A2:Z")

    _no_secret(err.value)
    assert err.value.live_refresh_token in (None, ROTATED)
