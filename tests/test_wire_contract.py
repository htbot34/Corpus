"""Schema-shape tests: the exact field names and nesting the code depends on.

The failure this exists to prevent is specific. Every reader in client.py and
providers.py has a fallback — ``_first(raw, "createdAt", "created_at",
"timestamp")``, ``_tweets_from`` probing four locations, ``_cursor_from``
falling back to ``bool(cursor)``. When a provider renames something, none of
those raise. The run finishes, reports zero posts, and exits 0.

So these tests assert the shape directly, and their failure messages say what
breaks rather than merely that an assertion failed.

A caveat these tests keep visible rather than hide: only ``user_info`` has been
confirmed against the live API. The tweet-endpoint fixtures are still synthetic,
so the tests below prove the code and the fixtures agree — not that either
matches twitterapi.io. ``test_unverified_endpoints_are_declared_unverified``
exists to stop that caveat from quietly disappearing.

Offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from corpus.x.client import _tweet_id, normalize_tweet, parse_created_at
from corpus.x.contract import (
    ADVANCED_SEARCH,
    CONTRACTS,
    CRITICAL,
    LAST_TWEETS,
    TWEETS_BY_IDS,
    USER_INFO,
    check_payload,
    locate_items,
    unverified_endpoints,
)
from corpus.x.providers import TwitterApiIoProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fmt(violations: list[Any]) -> str:
    return "\n".join(f"  {v}" for v in violations)


# --------------------------------------------------------------------------
# Contract vs fixtures
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contract", "fixture"),
    [
        (USER_INFO, "user_info.json"),
        (ADVANCED_SEARCH, "search_response.json"),
        (LAST_TWEETS, "last_tweets_response.json"),
        (TWEETS_BY_IDS, "batch_tweets_response.json"),
    ],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_fixture_satisfies_contract(contract: Any, fixture: str) -> None:
    violations = [v for v in check_payload(contract, load(fixture)) if v.severity == CRITICAL]
    assert not violations, f"{fixture} violates the {contract.name} contract:\n{_fmt(violations)}"


@pytest.mark.parametrize(
    ("contract", "fixture"),
    [
        (ADVANCED_SEARCH, "search_response.json"),
        (LAST_TWEETS, "last_tweets_response.json"),
        (TWEETS_BY_IDS, "batch_tweets_response.json"),
    ],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_item_array_is_where_the_code_looks_first(contract: Any, fixture: str) -> None:
    """_tweets_from probes in order; a match on a later probe is a warning sign."""
    location, items = locate_items(contract, load(fixture))
    assert location is not None, f"{fixture}: no tweet array found at all"
    assert items, f"{fixture}: tweet array is empty"
    assert location == contract.array_locations[0], (
        f"{fixture}: tweets are at {location!r}, contract says "
        f"{contract.array_locations[0]!r}. Still parsed, but docs/wire-contract.md "
        "is now wrong."
    )


def test_contract_detects_a_renamed_tweet_array() -> None:
    """The whole point: a shape change must be loud, not silently zero posts."""
    payload = {"status": "success", "items": load("search_response.json")["tweets"]}
    violations = check_payload(ADVANCED_SEARCH, payload)
    critical = [v for v in violations if v.severity == CRITICAL]
    assert critical
    assert "no item array found" in critical[0].message
    # The message has to be actionable on its own.
    assert "items" in critical[0].message  # lists the keys that ARE present


def test_contract_detects_a_renamed_timestamp_field() -> None:
    payload = load("search_response.json")
    for tweet in payload["tweets"]:
        tweet["created_at_utc"] = tweet.pop("createdAt")
    violations = [v for v in check_payload(ADVANCED_SEARCH, payload) if v.severity == CRITICAL]
    assert any("createdAt" in v.message for v in violations)
    assert any("burns budget making no progress" in v.message for v in violations)


def test_contract_detects_a_renamed_cursor_field() -> None:
    payload = dict(load("search_response.json"))
    payload["cursor_next"] = payload.pop("next_cursor", "abc")
    payload.pop("has_next_page", None)
    violations = check_payload(ADVANCED_SEARCH, payload)
    assert any("has_next_page" in v.message for v in violations)


# --------------------------------------------------------------------------
# Contract vs the code that reads it
# --------------------------------------------------------------------------
# The contract is only worth anything if it describes what the code actually
# does. These pin the two together.


@pytest.mark.parametrize(
    ("contract", "fixture"),
    [
        (ADVANCED_SEARCH, "search_response.json"),
        (LAST_TWEETS, "last_tweets_response.json"),
        (TWEETS_BY_IDS, "batch_tweets_response.json"),
    ],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_provider_extractor_agrees_with_contract(contract: Any, fixture: str) -> None:
    payload = load(fixture)
    _, expected = locate_items(contract, payload)
    actual = TwitterApiIoProvider._tweets_from(payload)
    assert [t.get("id") for t in actual] == [t.get("id") for t in expected], (
        f"{fixture}: contract.locate_items and providers._tweets_from disagree. "
        "One of them is describing a version of the code that no longer exists."
    )


def test_every_critical_field_is_actually_readable_by_normalize_tweet() -> None:
    """A contract that names fields normalize_tweet cannot read is decoration."""
    for tweet in load("search_response.json")["tweets"]:
        doc = normalize_tweet(tweet)
        assert doc.source_id, "normalize_tweet produced a document with no id"
        assert doc.published_at.year > 2005
        assert _tweet_id(tweet) == doc.source_id


def test_user_info_contract_matches_what_the_cli_reads() -> None:
    """cli.py reads statusesCount to size the run and the Phase 2.3 report."""
    profile = load("user_info.json")["data"]
    assert isinstance(profile.get("statusesCount"), int)
    assert profile["statusesCount"] > 0


# --------------------------------------------------------------------------
# Confirmed ground truth for user_info (live API, 2026-07-31)
# --------------------------------------------------------------------------


def test_user_info_envelope_includes_msg() -> None:
    """`msg` is on the wire. Its absence is how the fixtures were caught."""
    payload = load("user_info.json")
    assert list(payload.keys())[:3] == ["status", "msg", "data"]
    assert payload["msg"] == "success"


def test_user_info_created_at_is_iso_with_microseconds() -> None:
    """Confirmed real format: 2010-08-27T20:13:59.000000Z — not the legacy form."""
    raw = load("user_info.json")["data"]["createdAt"]
    assert raw.endswith("Z")
    assert "." in raw and len(raw.split(".")[1]) == 7  # 6 digits + "Z"
    assert parse_created_at(raw).tzinfo is not None


CONFIRMED_USER_INFO_KEYS = {
    "id",
    "name",
    "userName",
    "location",
    "url",
    "description",
    "entities",
    "protected",
    "isVerified",
    "isBlueVerified",
    "verifiedType",
    "followers",
    "following",
    "favouritesCount",
    "statusesCount",
    "mediaCount",
    "createdAt",
    "coverPicture",
    "profilePicture",
    "canDm",
    "affiliatesHighlightedLabel",
    "isAutomated",
    "automatedBy",
    "pinnedTweetIds",
}


def test_user_info_fixture_carries_the_confirmed_key_set() -> None:
    """Guards against the fixture drifting back toward the documented guess."""
    keys = set(load("user_info.json")["data"])
    missing = CONFIRMED_USER_INFO_KEYS - keys
    assert not missing, f"fixture lost confirmed live fields: {sorted(missing)}"


# --------------------------------------------------------------------------
# Timestamp formats
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The format the live API actually returns.
        ("2010-08-27T20:13:59.000000Z", datetime(2010, 8, 27, 20, 13, 59, tzinfo=timezone.utc)),
        # The legacy format, still accepted.
        ("Mon Mar 03 12:00:00 +0000 2014", datetime(2014, 3, 3, 12, 0, tzinfo=timezone.utc)),
    ],
)
def test_both_confirmed_timestamp_formats_parse(value: str, expected: datetime) -> None:
    assert parse_created_at(value) == expected


# --------------------------------------------------------------------------
# Honesty about what is and is not verified
# --------------------------------------------------------------------------


def test_unverified_endpoints_are_declared_unverified() -> None:
    """Fails deliberately when an endpoint's verified status changes.

    It has now fired twice, which is the point: once when user_info was
    confirmed (2026-07-31), and again when a live ingestion confirmed
    advanced_search and measured the tweets_by_ids batch ceiling (2026-08-02).
    Update this list and docs/wire-contract.md together.
    """
    assert unverified_endpoints() == ["last_tweets"], (
        "The set of unverified endpoints changed. If a capture or a live run "
        "confirmed one, update docs/wire-contract.md and this test together."
    )


def test_verified_endpoints_carry_their_evidence() -> None:
    """`verified` is a sentence about how, not a boolean. Keep it that way."""
    for name, contract in CONTRACTS.items():
        if not contract.is_verified:
            continue
        assert len(contract.verified) > 40, (
            f"{name}.verified is too terse to be evidence: {contract.verified!r}"
        )
        assert "2026-" in contract.verified, f"{name}.verified names no date"


def test_last_tweets_is_the_remaining_unverified_endpoint() -> None:
    verified = sorted(n for n, c in CONTRACTS.items() if c.is_verified)
    assert verified == ["advanced_search", "tweets_by_ids", "user_info"]
    assert "2026-07-31" in USER_INFO.verified


def test_the_batch_ceiling_is_recorded_in_the_contract() -> None:
    """Measured 2026-08-02 from a live 400. The docs said 100; it is 50."""
    from corpus.x.providers import BATCH_LOOKUP_MAX

    assert BATCH_LOOKUP_MAX == 50
    notes = " ".join(TWEETS_BY_IDS.notes)
    assert "50" in notes and "measured" in notes.lower()


def test_advanced_search_notes_the_cursor_cost_risk() -> None:
    """The bool(cursor) fallback is still the biggest open cost question."""
    notes = " ".join(ADVANCED_SEARCH.notes).lower()
    assert "last page" in notes and "cursor" in notes


# --------------------------------------------------------------------------
# The live checker must not rot between monthly runs
# --------------------------------------------------------------------------


def test_verify_contract_script_is_importable_and_offline_safe() -> None:
    """It runs about once a month; nothing else would catch it breaking."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_contract.py"
    spec = importlib.util.spec_from_file_location("verify_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # import alone must make no network call
    assert module.DEFAULT_HANDLE
    assert hasattr(module, "check_advanced_search")


def test_verify_contract_refuses_to_run_under_ci() -> None:
    """CI must stay offline and free."""
    import os
    import subprocess
    import sys as _sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_contract.py"
    result = subprocess.run(
        [_sys.executable, str(path)],
        env={**dict(os.environ), "CI": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "Refusing to run under CI" in result.stderr
