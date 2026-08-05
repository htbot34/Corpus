"""Spend tracking with a hard stop that is actually hard.

Every billable call is recorded: endpoint, units, unit cost, running total.
When the budget is exhausted the run stops and the partial corpus is still
written — paid data is never thrown away.

Reservations (why `charge()` alone is not a hard stop)
-----------------------------------------------------
`charge()` records a cost and *then* raises. By the time it raises, the call has
already completed and the money is already gone. On a large reduce call — say
`--reduce-model claude-opus-5`, `max_tokens=32_000`, roughly $3.50 on a
10,000-post corpus — that means a $10.00 budget sitting at $9.99 fires anyway
and lands at $13.49. A 35% overshoot on a flag documented as a hard stop.

The map stage is structurally worse: `MAP_CONCURRENCY = 4` under
`asyncio.gather`, each slice charging only after its call returns, so four calls
can be in flight before any of them has charged anything.

So: `reserve()` before the call, `settle()` after.

    with budget.reserve(estimated, "reduce") as r:
        message = await client.messages.create(...)
        r.settle(budget.charge_anthropic(...))

A reservation is counted against the limit for as long as it is outstanding, so
four concurrent map slices hold four reservations and the fifth cannot start if
the four together would overshoot. `charge()` keeps its post-hoc raise as a
backstop — reservations are estimates, and an estimate that is too low must
still stop the run, just later than we would like.

Modes
-----
`strict` (default) refuses any call it cannot fully reserve. `advisory`
reserves and reports but never blocks, preserving the original behaviour for
anyone who would rather overshoot than stop early.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

# --------------------------------------------------------------------------
# Price table
# --------------------------------------------------------------------------
# twitterapi.io, as published: $0.15 / 1,000 tweets, $0.18 / 1,000 profiles,
# with a minimum charge per request.
X_COST_PER_TWEET = 0.15 / 1000
X_COST_PER_PROFILE = 0.18 / 1000
X_MIN_CHARGE_PER_REQUEST = 0.00015

# Anthropic server-side web search, as published: $10 per 1,000 searches, on top
# of the tokens the search results consume. Billed per *search*, not per result
# and not per API call — `usage.server_tool_use.web_search_requests` is the unit,
# and a search that errors is not billed at all.
#
# At $0.01 a search this is the most expensive thing in the tool per call: one
# `--max-searches 12` run costs ~$0.12 in search fees before a single token. It
# is also the only cost in Phase 2 that a cache can remove entirely, which is
# why results are cached by query string.
SEARCH_COST_PER_QUERY = 10.00 / 1000

# Anthropic list prices, $ per million tokens.
# Claude Sonnet 5 carries introductory pricing ($2/$10) through 2026-08-31,
# after which it reverts to $3/$15. We charge the correct rate for today so the
# printed spend matches the invoice instead of being conveniently vague.
_SONNET_5_INTRO_END = date(2026, 8, 31)

MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),  # overridden below during the intro window
    # Both spellings: the map stage pins the dated id so a silent alias
    # re-point cannot change what we billed against mid-run.
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

# Prompt caching multipliers against the input rate.
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL
CACHE_READ_MULTIPLIER = 0.10


def model_rates(model: str, today: date | None = None) -> tuple[float, float]:
    """($ per 1M input, $ per 1M output) for a model, as of `today`."""
    today = today or date.today()
    if model == "claude-sonnet-5" and today <= _SONNET_5_INTRO_END:
        return (2.00, 10.00)
    if model not in MODEL_PRICES:
        raise KeyError(
            f"No price on file for model {model!r}. Add it to MODEL_PRICES rather "
            "than letting a run bill against an unknown rate."
        )
    return MODEL_PRICES[model]


class SpendSink(Protocol):
    """The slice of Cache that Budget actually uses.

    Typed structurally rather than importing Cache, so budget.py keeps no
    dependency on storage and a test can pass anything that records a charge.
    """

    def log_spend(
        self,
        run_id: str,
        category: str,
        endpoint: str,
        units: float,
        unit_cost: float,
        cost: float,
        note: str = "",
    ) -> None: ...


class BudgetExceeded(RuntimeError):
    """Raised when a charge would cross the hard limit."""


STRICT = "strict"
ADVISORY = "advisory"
BUDGET_MODES = (STRICT, ADVISORY)

# Reserved-vs-actual gap above which the estimator is considered wrong rather
# than merely imprecise. Logged, never fatal: an estimator that stops runs is
# worse than one that is quietly off by a quarter.
OVERSHOOT_ALARM = 0.20


@dataclass
class Charge:
    category: str  # "x" | "anthropic"
    endpoint: str
    units: float
    unit_cost: float
    cost: float
    note: str = ""


@dataclass
class Reservation:
    """A claim on budget held across an in-flight call.

    Outstanding reservations count against the limit, which is what stops four
    concurrent map slices from collectively overshooting: each holds its own
    estimate for the duration of its call.
    """

    budget: Budget
    endpoint: str
    estimated: float
    token: str
    settled: bool = False
    actual: float | None = None

    def settle(self, actual: float | None = None) -> None:
        """Release the reservation, reconciling against what was really spent.

        Call this *after* charging, not before: between the charge and the
        release both the real cost and the estimate count against the limit,
        which errs toward stopping early. The other order errs toward
        overshooting, which is the bug this class exists to fix.
        """
        if self.settled:
            return
        self.settled = True
        self.actual = actual
        self.budget._release(self.token)
        if actual is None:
            return
        if self.estimated > 0 and actual > self.estimated * (1 + OVERSHOOT_ALARM):
            self.budget.estimate_misses.append(
                f"{self.endpoint}: reserved ${self.estimated:.4f}, actually spent "
                f"${actual:.4f} ({actual / self.estimated - 1:+.0%}) — the "
                f"estimator is wrong, not just imprecise"
            )


@dataclass
class Budget:
    limit: float = 10.00
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    charges: list[Charge] = field(default_factory=list)
    cache: SpendSink | None = None  # corpus.cache.Cache, optional persistence
    stopped: bool = False
    mode: str = STRICT
    # Reservations held for in-flight calls, keyed by an opaque token.
    _reservations: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    estimate_misses: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    # Spend carried over from earlier attempts at the same target (--resume).
    # Counted against the limit, because otherwise `--budget 10` resumed three
    # times is a $30 run and the flag documented as a hard stop is per-attempt.
    prior_spend: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in BUDGET_MODES:
            raise ValueError(f"budget mode {self.mode!r} is not one of {BUDGET_MODES}")

    # -- totals -----------------------------------------------------------

    @property
    def total(self) -> float:
        return self.prior_spend + sum(c.cost for c in self.charges)

    @property
    def this_attempt(self) -> float:
        """Spend by this process alone, excluding anything carried by --resume."""
        return sum(c.cost for c in self.charges)

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.total)

    def total_for(self, category: str) -> float:
        return sum(c.cost for c in self.charges if c.category == category)

    def breakdown(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.charges:
            out[c.category] = out.get(c.category, 0.0) + c.cost
        return out

    # -- charging ---------------------------------------------------------

    def _record(self, charge: Charge) -> None:
        self.charges.append(charge)
        if self.cache is not None:
            self.cache.log_spend(
                self.run_id,
                charge.category,
                charge.endpoint,
                charge.units,
                charge.unit_cost,
                charge.cost,
                charge.note,
            )

    def charge(
        self,
        category: str,
        endpoint: str,
        units: float,
        unit_cost: float,
        note: str = "",
        minimum: float = 0.0,
    ) -> Charge:
        cost = max(units * unit_cost, minimum)
        charge = Charge(category, endpoint, units, unit_cost, cost, note)
        self._record(charge)
        if self.total >= self.limit:
            self.stopped = True
            raise BudgetExceeded(
                f"budget of ${self.limit:.2f} exhausted (spent ${self.total:.4f}) on {endpoint}"
            )
        return charge

    def charge_x_tweets(self, endpoint: str, count: int, note: str = "") -> Charge:
        return self.charge(
            "x",
            endpoint,
            count,
            X_COST_PER_TWEET,
            note=note,
            minimum=X_MIN_CHARGE_PER_REQUEST,
        )

    def charge_x_profiles(self, endpoint: str, count: int, note: str = "") -> Charge:
        return self.charge(
            "x",
            endpoint,
            count,
            X_COST_PER_PROFILE,
            note=note,
            minimum=X_MIN_CHARGE_PER_REQUEST,
        )

    def charge_searches(self, endpoint: str, count: int, note: str = "") -> Charge:
        """Record web searches actually performed.

        `count` comes from `usage.server_tool_use.web_search_requests`, not from
        how many queries we asked for: the model decides whether to search, and
        a search that errors is not billed. Charging what we requested rather
        than what happened would make the spend report a statement about our
        intentions.
        """
        return self.charge("search", endpoint, count, SEARCH_COST_PER_QUERY, note=note)

    def charge_anthropic(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        note: str = "",
    ) -> float:
        in_rate, out_rate = model_rates(model)
        cost = (
            input_tokens * in_rate / 1_000_000
            + output_tokens * out_rate / 1_000_000
            + cache_write_tokens * in_rate * CACHE_WRITE_MULTIPLIER / 1_000_000
            + cache_read_tokens * in_rate * CACHE_READ_MULTIPLIER / 1_000_000
        )
        total_units = input_tokens + output_tokens + cache_write_tokens + cache_read_tokens
        charge = Charge(
            "anthropic",
            model,
            total_units,
            cost / total_units if total_units else 0.0,
            cost,
            note
            or f"in={input_tokens} out={output_tokens} "
            f"cache_w={cache_write_tokens} cache_r={cache_read_tokens}",
        )
        self._record(charge)
        if self.total >= self.limit:
            self.stopped = True
            raise BudgetExceeded(
                f"budget of ${self.limit:.2f} exhausted (spent ${self.total:.4f}) on {model}"
            )
        return cost

    # -- reservations -----------------------------------------------------

    @property
    def reserved(self) -> float:
        """Total held by in-flight calls that have not yet charged."""
        with self._lock:
            return sum(self._reservations.values())

    @property
    def committed(self) -> float:
        """Spent plus reserved: what the run is already on the hook for."""
        return self.total + self.reserved

    def would_exceed(self, cost: float) -> bool:
        """Would spending `cost` cross the limit?

        Reservation-aware: outstanding reservations count, so this answers
        "given what is already in flight" rather than "given what has settled".
        With no reservations outstanding it is identical to the original
        total-based check.
        """
        return self.committed + cost > self.limit

    def reserve(self, estimated_cost: float, endpoint: str) -> Reservation:
        """Claim budget *before* making a call. Raises if it cannot be covered.

        This is the actual hard stop. `charge()` raising afterwards is a
        backstop for when the estimate was too low.
        """
        estimated_cost = max(0.0, float(estimated_cost))
        with self._lock:
            outstanding = sum(self._reservations.values())
            if self.total + outstanding + estimated_cost > self.limit:
                if self.mode == STRICT:
                    self.stopped = True
                    message = (
                        f"pre-flight: {endpoint} needs ~${estimated_cost:.4f}, but "
                        f"${self.total:.4f} is spent and ${outstanding:.4f} is "
                        f"reserved by calls in flight, against a ${self.limit:.2f} "
                        f"budget. Refusing the call rather than making it and "
                        f"reporting the overshoot afterwards. "
                        f"Use --budget-mode advisory to allow it."
                    )
                    self.refusals.append(f"{endpoint}: ~${estimated_cost:.4f}")
                    raise BudgetExceeded(message)
                self.refusals.append(
                    f"{endpoint}: ~${estimated_cost:.4f} would exceed the budget "
                    f"(advisory mode — allowed)"
                )
            token = uuid.uuid4().hex
            self._reservations[token] = estimated_cost
        return Reservation(self, endpoint, estimated_cost, token)

    def _release(self, token: str) -> None:
        with self._lock:
            self._reservations.pop(token, None)

    @contextmanager
    def reserved_for(self, estimated_cost: float, endpoint: str) -> Iterator[Reservation]:
        """`reserve` as a context manager: always releases, even on failure.

        A call that raises must not leave its reservation held — that would
        shrink the budget for the rest of the run every time something failed.
        """
        reservation = self.reserve(estimated_cost, endpoint)
        try:
            yield reservation
        finally:
            reservation.settle(reservation.actual)

    # -- reporting --------------------------------------------------------

    def summary_lines(self) -> list[str]:
        b = self.breakdown()
        lines = [
            f"X data (twitterapi.io): ${b.get('x', 0.0):.4f}",
            f"Web search:             ${b.get('search', 0.0):.4f}",
            f"Anthropic tokens:       ${b.get('anthropic', 0.0):.4f}",
            f"{'-' * 38}",
            f"Total:                  ${self.total:.4f} of ${self.limit:.2f} budget",
        ]
        if self.stopped:
            lines.append("STOPPED EARLY: budget exhausted, results are partial.")
        return lines


# --------------------------------------------------------------------------
# Pre-flight estimators
# --------------------------------------------------------------------------
# These feed `reserve()`. They are allowed to be wrong; they are not allowed to
# be optimistic, because an under-estimate is what lets an overshoot through.

# A search or timeline page. twitterapi.io returns 20 per page in the documented
# shape; UNVERIFIED against the wire (see docs/wire-contract.md), so it is named
# here rather than buried as a literal.
X_ASSUMED_PAGE_SIZE = 20

# Local token estimate. The Anthropic SDK exposes count_tokens, which is exact
# and free, and `estimate_anthropic_call` uses it when a real client is handed
# in. This is the fallback for the offline path.
#
# ERROR BAR: ~4 chars/token holds to about +/-15% on English prose. It
# under-counts on JSON (punctuation-dense, closer to 3 chars/token) and on
# non-Latin scripts, both of which appear in a corpus of posts. So the caller
# applies ESTIMATE_SAFETY_MARGIN on top, and any reservation that turns out
# more than 20% low is recorded in Budget.estimate_misses.
CHARS_PER_TOKEN_ESTIMATE = 4
ESTIMATE_SAFETY_MARGIN = 1.25


def estimate_tokens(text: str) -> int:
    """Rough local token count. Deliberately biased high — see the error bar."""
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE * ESTIMATE_SAFETY_MARGIN) + 1


def estimate_x_page_cost(page_size: int = X_ASSUMED_PAGE_SIZE) -> float:
    """Worst-case cost of one search/timeline page."""
    return max(page_size * X_COST_PER_TWEET, X_MIN_CHARGE_PER_REQUEST)


def estimate_x_batch_cost(id_count: int) -> float:
    """Worst-case cost of one batch hydration call: every id resolves."""
    return max(id_count * X_COST_PER_TWEET, X_MIN_CHARGE_PER_REQUEST)


def estimate_anthropic_call(
    model: str,
    input_tokens: int,
    max_output_tokens: int,
    cache_read_tokens: int = 0,
) -> float:
    """Worst-case cost of one model call, for reservation purposes.

    `max_output_tokens` is charged in full: the whole point of a reservation is
    that we do not know how much the model will actually emit, and the budget
    must hold even if it emits the maximum. Reconciliation afterwards gives the
    difference back.
    """
    in_rate, out_rate = model_rates(model)
    return (
        input_tokens * in_rate / 1_000_000
        + max_output_tokens * out_rate / 1_000_000
        + cache_read_tokens * in_rate * CACHE_READ_MULTIPLIER / 1_000_000
    )


def estimate_search_call(
    model: str,
    input_tokens: int,
    max_output_tokens: int,
    searches: int = 1,
) -> float:
    """Worst-case cost of one search call: the search fee plus the model call.

    The search fee dominates — $0.01 against a fraction of a cent of Haiku
    tokens — but both are reserved, because a reservation that covers only the
    larger half is not a ceiling.
    """
    return searches * SEARCH_COST_PER_QUERY + estimate_anthropic_call(
        model, input_tokens, max_output_tokens
    )


def estimate_search_phase(max_searches: int, model: str = "claude-haiku-4-5-20251001") -> float:
    """Pre-flight estimate for the whole discovery-by-search phase.

    Assumes every query runs (the cache makes a re-run cheaper, never dearer)
    and that each carries ~4k tokens of results back — search results are
    counted as input tokens, and they are the bulk of what a search call costs
    beyond the fee itself.
    """
    return max_searches * estimate_search_call(model, 4_000, 1_000)


def estimate_x_cost(post_count: int, hydration_ratio: float = 0.5) -> float:
    """Rough pre-flight estimate for a run.

    `hydration_ratio` is the fraction of posts expected to be replies or
    quotes, each of which needs one extra tweet read to fetch its parent.
    """
    reads = post_count * (1 + hydration_ratio)
    return reads * X_COST_PER_TWEET + X_COST_PER_PROFILE


def estimate_anthropic_split(
    post_count: int,
    map_model: str = "claude-haiku-4-5-20251001",
    reduce_model: str = "claude-sonnet-5",
) -> tuple[float, float]:
    """(map, reduce) separately, for the per-phase breakdown a run prints.

    Split out rather than summed inline because the two phases answer different
    questions when an estimate looks wrong: map scales with corpus size, reduce
    barely moves. A single number cannot tell you which one surprised you.
    """
    corpus_tokens = post_count * 120
    map_chunks = max(1, round(corpus_tokens / 30_000))
    map_in, map_out = model_rates(map_model)
    reduce_in_rate, reduce_out_rate = model_rates(reduce_model)
    map_cost = corpus_tokens * map_in / 1_000_000 + map_chunks * 1_500 * map_out / 1_000_000
    reduce_in = map_chunks * 1_500 + 3_000  # map outputs + signals.json
    reduce_cost = reduce_in * reduce_in_rate / 1_000_000 + 10_000 * reduce_out_rate / 1_000_000
    return map_cost, reduce_cost


def estimate_anthropic_cost(
    post_count: int,
    map_model: str = "claude-haiku-4-5-20251001",
    reduce_model: str = "claude-sonnet-5",
) -> float:
    """Rough pre-flight estimate for map+reduce over `post_count` documents.

    Assumes ~120 tokens/document, ~30k-token map chunks with ~1.5k-token JSON
    replies, and one reduce call over the map outputs plus signals. The reduce
    output allowance is generous because the reduce model thinks by default and
    thinking bills as output.

    Deliberately does not discount for the low-signal prefilter: a pre-flight
    estimate that runs high is a budget you do not blow through. It is also
    *not* the reservation — `estimate_anthropic_call` is, and it charges the
    full `max_tokens` because a reservation that guesses low is not a ceiling.
    """
    return sum(estimate_anthropic_split(post_count, map_model, reduce_model))
