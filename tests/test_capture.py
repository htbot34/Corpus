"""--capture-raw: the Phase 1 evidence-gathering path.

These tests drive the *real* TwitterApiIoProvider through an httpx MockTransport
rather than a fake provider object. That matters: the whole point of capture is
to record what `_get` saw before `_tweets_from` and `_cursor_from` interpret it,
so a test that stubs out the provider would test nothing.

Offline: MockTransport never opens a socket.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from corpus.redact import MASK
from corpus.x.capture import RawCapture
from corpus.x.providers import ProviderError, TwitterApiIoProvider

FAKE_KEY = "new" + "1_" + "z9Y8x7W6v5U4t3S2r1Q0p9O8n7M6l5K4"  # secrets-scan: ok

SAMPLE = {
    "status": "success",
    "msg": "success",
    "data": {"userName": "paulg", "id": "183749519", "statusesCount": 53901},
}


def _provider(
    handler: object, capture: RawCapture | None = None
) -> TwitterApiIoProvider:
    provider = TwitterApiIoProvider(api_key=FAKE_KEY, capture=capture)
    provider.client = httpx.Client(
        base_url=provider.base_url,
        headers={"X-API-Key": FAKE_KEY},
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return provider


def test_capture_writes_one_file_per_call(tmp_path: Path) -> None:
    capture = RawCapture(tmp_path / "captures")
    provider = _provider(lambda request: httpx.Response(200, json=SAMPLE), capture)

    provider.user_info("paulg")
    provider.user_info("pmarca")

    files = sorted((tmp_path / "captures").glob("*.json"))
    assert len(files) == 2
    for f in files:
        assert f.name.startswith("twitter_user_info_")
        assert ":" not in f.name  # must be a legal Windows filename


def test_capture_body_is_verbatim_and_provable(tmp_path: Path) -> None:
    """The captured body must be the wire payload, not our reading of it."""
    raw_bytes = json.dumps(SAMPLE).encode()
    capture = RawCapture(tmp_path)
    provider = _provider(
        lambda request: httpx.Response(
            200, content=raw_bytes, headers={"content-type": "application/json"}
        ),
        capture,
    )
    provider.user_info("paulg")

    envelope = json.loads(next(tmp_path.glob("*.json")).read_text())
    # Byte-identical: the digest is over what arrived, so a normalization step
    # sneaking in later would break this test rather than pass silently.
    assert envelope["response"]["body_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert envelope["response"]["body_redacted"] is False  # digest is meaningful
    assert envelope["response"]["body"] == SAMPLE
    # The envelope must not have dropped the fields the contract turns on.
    assert envelope["response"]["body"]["msg"] == "success"
    assert list(envelope["response"]["body"].keys()) == ["status", "msg", "data"]


def test_capture_records_request_shape_and_response_headers(tmp_path: Path) -> None:
    """Phase 1.3 needs param names; Phase 2.4 needs the rate-limit headers."""
    capture = RawCapture(tmp_path)
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json=SAMPLE,
            headers={"Retry-After": "30", "x-rate-limit-reset": "1769900000"},
        ),
        capture,
    )
    provider.advanced_search("from:paulg since_time:1 until_time:2")

    envelope = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert envelope["request"]["path"] == "/twitter/tweet/advanced_search"
    assert set(envelope["request"]["params"]) == {"query", "queryType"}
    assert envelope["response"]["headers"]["retry-after"] == "30"
    assert envelope["response"]["headers"]["x-rate-limit-reset"] == "1769900000"


def test_capture_never_writes_the_api_key(tmp_path: Path) -> None:
    """Capture dirs get zipped and shared. Request headers must not be in them."""
    capture = RawCapture(tmp_path)
    provider = _provider(
        lambda request: httpx.Response(
            # An upstream that echoes the key back at us, which is the whole
            # reason redaction exists.
            401,
            json={"error": "bad key", "sent": FAKE_KEY},
            headers={"x-echo-key": FAKE_KEY},
        ),
        capture,
    )
    with pytest.raises(ProviderError):
        provider.user_info("paulg")

    text = next(tmp_path.glob("*.json")).read_text()
    assert FAKE_KEY not in text
    assert MASK in text
    # Flagged, because body_sha256 no longer describes the stored body.
    assert json.loads(text)["response"]["body_redacted"] is True


def test_capture_records_error_responses(tmp_path: Path) -> None:
    """A 4xx body is the most informative capture there is. Keep it."""
    capture = RawCapture(tmp_path)
    provider = _provider(
        lambda request: httpx.Response(404, json={"status": "error", "msg": "not found"}),
        capture,
    )
    with pytest.raises(ProviderError):
        provider.user_info("nosuchuser")

    envelope = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert envelope["response"]["status"] == 404
    assert envelope["response"]["body"]["msg"] == "not found"


def test_capture_keeps_non_json_bodies(tmp_path: Path) -> None:
    """A non-JSON response IS a finding; losing it defeats the purpose."""
    capture = RawCapture(tmp_path)
    provider = _provider(
        lambda request: httpx.Response(200, content=b"<html>cloudflare</html>"), capture
    )
    with pytest.raises(ProviderError):
        provider.user_info("paulg")

    envelope = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert envelope["response"]["body"] == "<html>cloudflare</html>"
    assert envelope["response"]["body_parse_error"]


def test_capture_failure_does_not_break_the_run(tmp_path: Path) -> None:
    """A capture problem must never cost data the user already paid for."""
    capture = RawCapture(tmp_path)
    capture.directory = tmp_path / "deleted"  # never created: writes will fail
    logged: list[str] = []
    capture.log = logged.append
    provider = _provider(lambda request: httpx.Response(200, json=SAMPLE), capture)

    assert provider.user_info("paulg")["userName"] == "paulg"
    assert logged and "capture" in logged[0]


def test_no_capture_configured_is_a_no_op(tmp_path: Path) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=SAMPLE), None)
    assert provider.user_info("paulg")["id"] == "183749519"
    assert not list(tmp_path.glob("*.json"))
