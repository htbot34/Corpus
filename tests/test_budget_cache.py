"""Budget guard and cache behaviour. Spend must be visible and bounded."""

from __future__ import annotations

import time
from datetime import date

import pytest

from corpus.budget import (
    Budget,
    BudgetExceeded,
    estimate_anthropic_cost,
    estimate_x_cost,
    model_rates,
)
from corpus.cache import Cache

# -- budget -----------------------------------------------------------------


def test_tweet_reads_are_priced_at_15_cents_per_thousand(cache):
    b = Budget(limit=10.0, cache=cache)
    b.charge_x_tweets("advanced_search", 1000)
    assert b.total == pytest.approx(0.15)


def test_profile_reads_are_priced_at_18_cents_per_thousand(cache):
    b = Budget(limit=10.0, cache=cache)
    b.charge_x_profiles("user/info", 1000)
    assert b.total == pytest.approx(0.18)


def test_minimum_charge_applies_to_tiny_requests(cache):
    b = Budget(limit=10.0, cache=cache)
    b.charge_x_tweets("advanced_search", 1)
    assert b.total == pytest.approx(0.00015)


def test_hard_stop_raises_and_marks_stopped(cache):
    b = Budget(limit=0.05, cache=cache)
    with pytest.raises(BudgetExceeded):
        for _ in range(100):
            b.charge_x_tweets("advanced_search", 100)
    assert b.stopped
    assert b.total >= 0.05


def test_anthropic_charge_uses_published_rates(cache):
    b = Budget(limit=100.0, cache=cache)
    cost = b.charge_anthropic("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(5.0)
    cost = b.charge_anthropic("claude-opus-5", input_tokens=0, output_tokens=1_000_000)
    assert cost == pytest.approx(25.0)


def test_cache_reads_are_a_tenth_and_writes_are_1_25x(cache):
    b = Budget(limit=100.0, cache=cache)
    read = b.charge_anthropic("claude-opus-5", 0, 0, cache_read_tokens=1_000_000)
    write = b.charge_anthropic("claude-opus-5", 0, 0, cache_write_tokens=1_000_000)
    assert read == pytest.approx(0.5)
    assert write == pytest.approx(6.25)


def test_sonnet_intro_pricing_is_date_aware():
    assert model_rates("claude-sonnet-5", date(2026, 8, 1)) == (2.00, 10.00)
    assert model_rates("claude-sonnet-5", date(2026, 9, 1)) == (3.00, 15.00)


def test_unknown_model_refuses_to_guess():
    with pytest.raises(KeyError):
        model_rates("claude-imaginary-9")


def test_breakdown_separates_x_from_anthropic(cache):
    b = Budget(limit=100.0, cache=cache)
    b.charge_x_tweets("advanced_search", 1000)
    b.charge_anthropic("claude-opus-5", 1_000_000, 0)
    breakdown = b.breakdown()
    assert breakdown["x"] == pytest.approx(0.15)
    assert breakdown["anthropic"] == pytest.approx(5.0)
    assert "X data" in b.summary_lines()[0]


def test_every_charge_is_logged_to_the_spend_table(cache):
    b = Budget(limit=100.0, cache=cache)
    b.charge_x_tweets("advanced_search", 500, note="window 1")
    b.charge_anthropic("claude-sonnet-5", 1000, 200)
    rows = cache.spend_log()
    assert len(rows) == 2
    assert {r["category"] for r in rows} == {"x", "anthropic"}
    assert any(r["note"] == "window 1" for r in rows)


def test_estimates_are_in_the_right_order_of_magnitude():
    # 3,000 posts of X data should cost well under a dollar
    assert 0.3 < estimate_x_cost(3000) < 1.0
    assert estimate_anthropic_cost(3000) > estimate_anthropic_cost(500)


# -- cache ------------------------------------------------------------------


def test_roundtrip(cache):
    cache.put("x", "tweet:1", {"text": "hello"})
    assert cache.get("x", "tweet:1") == {"text": "hello"}


def test_miss_returns_none(cache):
    assert cache.get("x", "nope") is None


def test_ttl_expiry(tmp_path):
    c = Cache(path=tmp_path / "c.db", ttl_seconds=0)
    c.put("x", "tweet:1", {"a": 1})
    time.sleep(0.01)
    assert c.get("x", "tweet:1") is None
    c.close()


def test_permanent_entries_ignore_ttl(tmp_path):
    c = Cache(path=tmp_path / "c.db", ttl_seconds=0)
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    time.sleep(0.01)
    assert c.get("x", "tweet:1") == {"a": 1}
    c.close()


def test_permanent_flag_is_never_downgraded(tmp_path):
    c = Cache(path=tmp_path / "c.db", ttl_seconds=0)
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    c.put("x", "tweet:1", {"a": 2})  # a later TTL'd write
    time.sleep(0.01)
    assert c.get("x", "tweet:1") == {"a": 2}, "must stay permanent"
    c.close()


def test_refresh_bypasses_reads(tmp_path):
    c = Cache(path=tmp_path / "c.db")
    c.put("x", "tweet:1", {"a": 1})
    c.close()
    c2 = Cache(path=tmp_path / "c.db", refresh=True)
    assert c2.get("x", "tweet:1") is None
    c2.close()


def test_offline_serves_expired_entries(tmp_path):
    c = Cache(path=tmp_path / "c.db", ttl_seconds=0)
    c.put("x", "tweet:1", {"a": 1})
    c.close()
    time.sleep(0.01)
    c2 = Cache(path=tmp_path / "c.db", ttl_seconds=0, offline=True)
    assert c2.get("x", "tweet:1") == {"a": 1}
    c2.close()


def test_clear_can_keep_permanent_entries(tmp_path):
    c = Cache(path=tmp_path / "c.db")
    c.put("x", "tweet:1", {"a": 1}, permanent=True)
    c.put("x", "corpus:x", [1, 2, 3])
    removed = c.clear(keep_permanent=True)
    assert removed == 1
    assert c.get("x", "tweet:1") is not None
    c.close()


def test_stats_report_counts_by_source(cache):
    cache.put("x", "tweet:1", {"a": 1}, permanent=True)
    cache.put("rss", "feed:1", "<xml/>")
    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["by_source"]["x"]["permanent"] == 1
