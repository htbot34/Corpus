"""Phase 2.4: retry that reads what the server said.

The old policy was `time.sleep(2**attempt)` on any 429 or 5xx, and on any
transport error at all. Three problems, all of which cost either money or time:

* it ignored Retry-After and x-rate-limit-reset, so it guessed when the server
  had already said;
* it had no jitter, so concurrent retries resynchronised into the same
  thundering herd that caused the rate limit;
* it could not tell a transient failure from a permanent one, so a spent
  account balance or a proxy policy denial burned every attempt before
  surfacing an error that was never going to change.

`time.sleep` is patched out in every test here — the suite must stay fast, and
what is under test is the *decision*, not the waiting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from corpus.x import providers as P
from corpus.x.providers import (
    BASE_BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    MAX_SLEEP_SECONDS,
    ProviderError,
    TwitterApiIoProvider,
    _backoff_delay,
    _is_quota_exhausted,
    _is_retryable_transport_error,
    _retry_delay,
    _server_directed_delay,
)

FAKE_KEY = "new" + "1_" + "q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6"  # secrets-scan: ok
OK_BODY = {"status": "success", "msg": "success", "data": {"userName": "paulg"}}


@pytest.fixture()
def no_sleep(monkeypatch):
    """Record what would have been slept, without sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(P.time, "sleep", lambda s: slept.append(s))
    return slept


def build(handler, logs: list[str] | None = None) -> TwitterApiIoProvider:
    provider = TwitterApiIoProvider(
        api_key=FAKE_KEY, log=(logs.append if logs is not None else (lambda _m: None))
    )
    provider.client = httpx.Client(
        base_url=provider.base_url,
        headers={"X-API-Key": FAKE_KEY},
        transport=httpx.MockTransport(handler),
    )
    return provider


def responder(*responses: httpx.Response):
    """Serve the given responses in order, repeating the last one."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


# -- honouring the server's instruction ------------------------------------


def test_retry_after_seconds_is_honoured(no_sleep):
    provider = build(
        responder(
            httpx.Response(429, headers={"Retry-After": "30"}, text="slow down"),
            httpx.Response(200, json=OK_BODY),
        )
    )
    provider.user_info("paulg")
    assert no_sleep, "no retry happened"
    # 30s plus up to 10% jitter, and nothing like the old 2**0 == 1s guess.
    assert 30.0 <= no_sleep[0] <= 33.0


def test_retry_after_http_date_is_honoured(no_sleep):
    when = datetime.now(tz=timezone.utc) + timedelta(seconds=45)
    provider = build(
        responder(
            httpx.Response(429, headers={"Retry-After": format_datetime(when)}, text="wait"),
            httpx.Response(200, json=OK_BODY),
        )
    )
    provider.user_info("paulg")
    assert 40.0 <= no_sleep[0] <= 50.0


@pytest.mark.parametrize("header", ["x-rate-limit-reset", "x-ratelimit-reset", "ratelimit-reset"])
def test_rate_limit_reset_timestamp_is_honoured(header: str, no_sleep):
    reset = datetime.now(tz=timezone.utc) + timedelta(seconds=25)
    provider = build(
        responder(
            httpx.Response(429, headers={header: str(int(reset.timestamp()))}, text="wait"),
            httpx.Response(200, json=OK_BODY),
        )
    )
    provider.user_info("paulg")
    assert 20.0 <= no_sleep[0] <= 30.0


def test_a_small_reset_value_is_read_as_a_delta():
    resp = httpx.Response(429, headers={"x-rate-limit-reset": "12"})
    assert _server_directed_delay(resp) == pytest.approx(12.0, abs=1.0)


def test_a_large_reset_value_is_read_as_an_absolute_time():
    future = datetime.now(tz=timezone.utc) + timedelta(seconds=90)
    resp = httpx.Response(429, headers={"x-rate-limit-reset": str(int(future.timestamp()))})
    assert _server_directed_delay(resp) == pytest.approx(90.0, abs=2.0)


def test_a_past_reset_never_sleeps_negative():
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=500)
    resp = httpx.Response(429, headers={"x-rate-limit-reset": str(int(past.timestamp()))})
    assert _server_directed_delay(resp) == 0.0


def test_no_headers_falls_back_to_backoff():
    assert _server_directed_delay(httpx.Response(429)) is None


def test_server_delay_is_still_capped():
    """A provider asking for an hour must not hang the run for an hour."""
    resp = httpx.Response(429, headers={"Retry-After": "100000"})
    assert _retry_delay(resp, 0) == MAX_SLEEP_SECONDS


# -- jitter ----------------------------------------------------------------


def test_backoff_has_jitter():
    """Undithered backoff makes concurrent retries fire in unison."""
    samples = {_backoff_delay(4) for _ in range(50)}
    assert len(samples) > 40, "delays are not jittered"


def test_backoff_grows_with_attempt():
    early = max(_backoff_delay(0) for _ in range(200))
    late = max(_backoff_delay(5) for _ in range(200))
    assert late > early


def test_backoff_is_capped():
    assert all(_backoff_delay(50) <= MAX_SLEEP_SECONDS for _ in range(100))


def test_backoff_ceiling_matches_the_documented_base():
    assert max(_backoff_delay(0) for _ in range(500)) <= BASE_BACKOFF_SECONDS


# -- hard failures fail fast -----------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"insufficient balance"}',
        '{"msg":"out of credits"}',
        '{"error":"quota exceeded, please top up"}',
        "Payment Required: recharge your account",
    ],
)
def test_exhausted_balance_fails_immediately(body: str, no_sleep):
    provider = build(responder(httpx.Response(429, text=body)))
    with pytest.raises(ProviderError) as exc:
        provider.user_info("paulg")
    assert no_sleep == [], "a spent balance must not be retried"
    assert "balance is exhausted" in str(exc.value)
    assert "twitterapi.io" in str(exc.value), "the message must say what to do"


def test_ordinary_rate_limiting_is_still_retried(no_sleep):
    provider = build(
        responder(
            httpx.Response(429, text="too many requests, slow down"),
            httpx.Response(200, json=OK_BODY),
        )
    )
    assert provider.user_info("paulg")["userName"] == "paulg"
    assert len(no_sleep) == 1


def test_quota_detection_does_not_fire_on_plain_rate_limits():
    assert not _is_quota_exhausted(httpx.Response(429, text="rate limit exceeded"))
    assert _is_quota_exhausted(httpx.Response(429, text="insufficient balance"))


def test_proxy_denial_is_not_retried(no_sleep):
    """Exactly the failure this environment produces: 403 to CONNECT."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("403 Forbidden")

    provider = build(handler)
    with pytest.raises(ProviderError) as exc:
        provider.user_info("paulg")
    assert no_sleep == [], "a proxy policy denial fails identically every time"
    assert "not a transient failure" in str(exc.value)


def test_dns_failure_is_not_retried(no_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    provider = build(handler)
    with pytest.raises(ProviderError):
        provider.user_info("paulg")
    assert no_sleep == []


def test_timeouts_are_retried(no_sleep):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=OK_BODY)

    provider = build(handler)
    assert provider.user_info("paulg")["userName"] == "paulg"
    assert len(no_sleep) == 2


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (httpx.ReadTimeout("t"), True),
        (httpx.ConnectTimeout("t"), True),
        (httpx.RemoteProtocolError("t"), True),
        (httpx.ProxyError("403"), False),
        (httpx.UnsupportedProtocol("nope"), False),
        (httpx.ConnectError("Name or service not known"), False),
        (httpx.ConnectError("connection refused"), True),
    ],
)
def test_transport_error_classification(exc: Exception, retryable: bool):
    assert _is_retryable_transport_error(exc) is retryable


# -- attempts and logging --------------------------------------------------


def test_attempts_are_capped_at_five(no_sleep):
    provider = build(responder(httpx.Response(503, text="upstream down")))
    with pytest.raises(ProviderError):
        provider.user_info("paulg")
    assert len(no_sleep) == MAX_ATTEMPTS
    assert MAX_ATTEMPTS == 5


def test_every_retry_is_logged_with_attempt_and_reason(no_sleep):
    logs: list[str] = []
    provider = build(
        responder(
            httpx.Response(503, text="upstream down"),
            httpx.Response(200, json=OK_BODY),
        ),
        logs=logs,
    )
    provider.user_info("paulg")
    assert logs, "silent retries are how a run appears to hang"
    assert "[retry 1/5]" in logs[0]
    assert "503" in logs[0]
    assert "sleeping" in logs[0]


def test_server_directed_retries_say_so(no_sleep):
    logs: list[str] = []
    provider = build(
        responder(
            httpx.Response(429, headers={"Retry-After": "5"}, text="slow"),
            httpx.Response(200, json=OK_BODY),
        ),
        logs=logs,
    )
    provider.user_info("paulg")
    assert "server-directed" in logs[0]


def test_auth_failures_are_never_retried(no_sleep):
    provider = build(responder(httpx.Response(401, text="bad key")))
    with pytest.raises(ProviderError) as exc:
        provider.user_info("paulg")
    assert no_sleep == []
    assert "X_API_KEY" in str(exc.value)


def test_retry_messages_never_leak_the_key(no_sleep):
    logs: list[str] = []
    provider = build(responder(httpx.Response(503, text=f"upstream saw {FAKE_KEY}")), logs=logs)
    with pytest.raises(ProviderError) as exc:
        provider.user_info("paulg")
    assert FAKE_KEY not in str(exc.value)
    assert all(FAKE_KEY not in line for line in logs)
