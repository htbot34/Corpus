"""Phase 2.1: the budget is a ceiling, not a tripwire.

`charge()` records a cost and then raises — by which point the call is done and
the money is gone. These tests pin the pre-flight behaviour that replaces it,
and in particular the concurrency case, which is where the old design was
structurally unable to hold the line: four map slices under `asyncio.gather`,
each charging only on return, could all be in flight before any had charged.

Offline.
"""

from __future__ import annotations

import asyncio

import pytest

from corpus.budget import (
    ADVISORY,
    Budget,
    BudgetExceeded,
    STRICT,
    estimate_anthropic_call,
    estimate_tokens,
    estimate_x_batch_cost,
    estimate_x_page_cost,
)


# -- basics -----------------------------------------------------------------


def test_reserve_blocks_before_the_call_not_after(cache):
    b = Budget(limit=1.00, cache=cache)
    with pytest.raises(BudgetExceeded) as exc:
        b.reserve(1.50, "reduce")
    assert "pre-flight" in str(exc.value)
    assert b.total == 0.0, "a refused call must not be billed"
    assert b.stopped


def test_reserve_permits_what_fits(cache):
    b = Budget(limit=1.00, cache=cache)
    r = b.reserve(0.50, "reduce")
    assert b.reserved == pytest.approx(0.50)
    assert b.committed == pytest.approx(0.50)
    r.settle(0.40)
    assert b.reserved == 0.0


def test_outstanding_reservations_count_against_the_limit(cache):
    b = Budget(limit=1.00, cache=cache)
    b.reserve(0.60, "map 1")
    with pytest.raises(BudgetExceeded):
        b.reserve(0.60, "map 2")


def test_settling_frees_the_reservation(cache):
    b = Budget(limit=1.00, cache=cache)
    first = b.reserve(0.60, "map 1")
    first.settle(0.10)
    b.charge("anthropic", "map 1", 1, 0.10)
    # 0.10 spent, nothing reserved -> 0.60 is affordable again.
    b.reserve(0.60, "map 2")


def test_reservation_is_released_even_when_the_call_raises(cache):
    """A failed call must not shrink the budget for the rest of the run."""
    b = Budget(limit=1.00, cache=cache)
    with pytest.raises(RuntimeError):
        with b.reserved_for(0.50, "map 1"):
            raise RuntimeError("upstream blew up")
    assert b.reserved == 0.0
    b.reserve(0.90, "map 2")  # must not raise


def test_would_exceed_accounts_for_in_flight_reservations(cache):
    b = Budget(limit=1.00, cache=cache)
    assert not b.would_exceed(0.90)
    b.reserve(0.50, "map 1")
    assert b.would_exceed(0.90)
    assert not b.would_exceed(0.40)


def test_would_exceed_matches_the_old_behaviour_with_no_reservations(cache):
    b = Budget(limit=1.00, cache=cache)
    b.charge("x", "search", 1, 0.30)
    assert b.would_exceed(0.71)
    assert not b.would_exceed(0.69)


# -- modes ------------------------------------------------------------------


def test_advisory_mode_never_blocks(cache):
    b = Budget(limit=0.01, cache=cache, mode=ADVISORY)
    b.reserve(5.00, "reduce")  # must not raise
    assert b.refusals and "advisory" in b.refusals[0]


def test_strict_is_the_default(cache):
    assert Budget(limit=1.0, cache=cache).mode == STRICT


def test_unknown_mode_is_rejected(cache):
    with pytest.raises(ValueError):
        Budget(limit=1.0, cache=cache, mode="permissive")


# -- reconciliation ---------------------------------------------------------


def test_overshoot_beyond_20_percent_is_recorded(cache):
    b = Budget(limit=10.0, cache=cache)
    r = b.reserve(1.00, "reduce")
    r.settle(1.50)  # 50% over
    assert b.estimate_misses
    assert "estimator is wrong" in b.estimate_misses[0]


def test_small_estimate_error_is_not_flagged(cache):
    b = Budget(limit=10.0, cache=cache)
    r = b.reserve(1.00, "reduce")
    r.settle(1.10)  # 10% over: imprecise, not wrong
    assert not b.estimate_misses


def test_settle_is_idempotent(cache):
    b = Budget(limit=10.0, cache=cache)
    r = b.reserve(1.00, "reduce")
    r.settle(0.50)
    r.settle(0.50)
    assert b.reserved == 0.0
    assert not b.estimate_misses


# -- concurrency: the case the old design could not hold --------------------


def test_four_concurrent_slices_cannot_collectively_overshoot(cache):
    """MAP_CONCURRENCY=4 with charge-on-return was the structural hole.

    Four slices at $0.30 each against a $1.00 budget: three fit, the fourth must
    be refused *before* its call, not discovered afterwards.
    """
    b = Budget(limit=1.00, cache=cache)
    started: list[int] = []
    refused: list[int] = []

    async def slice_call(index: int) -> None:
        try:
            reservation = b.reserve(0.30, f"map {index}")
        except BudgetExceeded:
            refused.append(index)
            return
        started.append(index)
        await asyncio.sleep(0.01)  # in flight, not yet charged
        b.charge("anthropic", f"map {index}", 1, 0.30)
        reservation.settle(0.30)

    async def main() -> None:
        await asyncio.gather(*(slice_call(i) for i in range(4)))

    asyncio.run(main())

    assert len(started) == 3, f"expected 3 to fit, got {started}"
    assert len(refused) == 1
    assert b.total <= 1.00


def test_concurrent_reservations_are_thread_safe(cache):
    """charge() is reachable from sync code; the reservation table needs a lock."""
    import threading

    b = Budget(limit=1.00, cache=cache)
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            b.reserve(0.10, "concurrent")
            ok = True
        except BudgetExceeded:
            ok = False
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly ten $0.10 reservations fit in $1.00 — no more, no fewer.
    assert sum(granted) == 10, f"granted {sum(granted)} reservations against a $1.00 budget"
    assert b.reserved == pytest.approx(1.00)


# -- estimators -------------------------------------------------------------


def test_page_estimate_uses_the_assumed_page_size():
    assert estimate_x_page_cost(20) == pytest.approx(20 * 0.15 / 1000)


def test_page_estimate_respects_the_minimum_charge():
    assert estimate_x_page_cost(1) == pytest.approx(0.00015)


def test_batch_estimate_assumes_every_id_resolves():
    assert estimate_x_batch_cost(100) == pytest.approx(100 * 0.15 / 1000)


def test_anthropic_estimate_charges_max_tokens_as_worst_case():
    """A reservation must hold even if the model emits the maximum."""
    cost = estimate_anthropic_call("claude-opus-5", input_tokens=0, max_output_tokens=32_000)
    assert cost == pytest.approx(32_000 * 25.00 / 1_000_000)


def test_token_estimate_is_biased_high():
    """Under-counting is what lets an overshoot through, so it must never do that."""
    text = "x" * 4000  # 1,000 tokens at 4 chars/token
    assert estimate_tokens(text) >= 1000


def test_reduce_reservation_covers_the_documented_worst_case(cache):
    """The 10,000-post reduce that motivated this: ~$3.50, must not slip through."""
    b = Budget(limit=10.00, cache=cache)
    b.charge("x", "ingest", 1, 9.99)  # the pathological case from the brief
    estimate = estimate_anthropic_call("claude-opus-5", 200_000, 32_000)
    assert estimate > 1.00, "the reduce call is expensive; the estimate must say so"
    with pytest.raises(BudgetExceeded):
        b.reserve(estimate, "reduce")
    assert b.total == pytest.approx(9.99), "still under the limit, not $13.49"


# -- client integration -----------------------------------------------------


QUERY = "from:testsubject since_time:1704672000 until_time:1725148800"


def test_client_learns_the_real_page_size(client, provider):
    """A pessimistic estimate refuses calls that would have fit."""
    before = client._page_reservation()
    client.advanced_search(QUERY)
    after = client._page_reservation()
    assert before == pytest.approx(estimate_x_page_cost(20))
    assert after < before, "reservation should shrink once the real page size is known"


def test_client_reserves_before_calling_the_provider(cache, provider):
    """The provider must never be touched when the budget cannot cover it."""
    budget = Budget(limit=0.0001, cache=cache)
    from corpus.x.client import XClient

    client = XClient(provider, cache, budget)
    with pytest.raises(BudgetExceeded):
        client.advanced_search(QUERY)
    assert provider.calls == [], "a refused call must not reach the provider"
