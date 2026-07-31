"""Spend tracking with a hard stop.

Every billable call is recorded: endpoint, units, unit cost, running total.
When the budget is exhausted the run stops and the partial corpus is still
written — paid data is never thrown away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

# --------------------------------------------------------------------------
# Price table
# --------------------------------------------------------------------------
# twitterapi.io, as published: $0.15 / 1,000 tweets, $0.18 / 1,000 profiles,
# with a minimum charge per request.
X_COST_PER_TWEET = 0.15 / 1000
X_COST_PER_PROFILE = 0.18 / 1000
X_MIN_CHARGE_PER_REQUEST = 0.00015

# Anthropic list prices, $ per million tokens.
# Claude Sonnet 5 carries introductory pricing ($2/$10) through 2026-08-31,
# after which it reverts to $3/$15. We charge the correct rate for today so the
# printed spend matches the invoice instead of being conveniently vague.
_SONNET_5_INTRO_END = date(2026, 8, 31)

MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),  # overridden below during the intro window
    "claude-haiku-4-5": (1.00, 5.00),
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


class BudgetExceeded(RuntimeError):
    """Raised when a charge would cross the hard limit."""


@dataclass
class Charge:
    category: str  # "x" | "anthropic"
    endpoint: str
    units: float
    unit_cost: float
    cost: float
    note: str = ""


@dataclass
class Budget:
    limit: float = 10.00
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    charges: list[Charge] = field(default_factory=list)
    cache: object | None = None  # corpus.cache.Cache, optional persistence
    stopped: bool = False

    # -- totals -----------------------------------------------------------

    @property
    def total(self) -> float:
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
                f"budget of ${self.limit:.2f} exhausted (spent ${self.total:.4f}) "
                f"on {endpoint}"
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

    def would_exceed(self, cost: float) -> bool:
        return self.total + cost > self.limit

    # -- reporting --------------------------------------------------------

    def summary_lines(self) -> list[str]:
        b = self.breakdown()
        lines = [
            f"X data (twitterapi.io): ${b.get('x', 0.0):.4f}",
            f"Anthropic tokens:       ${b.get('anthropic', 0.0):.4f}",
            f"{'-' * 38}",
            f"Total:                  ${self.total:.4f} of ${self.limit:.2f} budget",
        ]
        if self.stopped:
            lines.append("STOPPED EARLY: budget exhausted, results are partial.")
        return lines


def estimate_x_cost(post_count: int, hydration_ratio: float = 0.5) -> float:
    """Rough pre-flight estimate for a run.

    `hydration_ratio` is the fraction of posts expected to be replies or
    quotes, each of which needs one extra tweet read to fetch its parent.
    """
    reads = post_count * (1 + hydration_ratio)
    return reads * X_COST_PER_TWEET + X_COST_PER_PROFILE


def estimate_anthropic_cost(post_count: int) -> float:
    """Rough pre-flight estimate for map+reduce over `post_count` documents.

    Assumes ~120 tokens/document, ~30k-token map chunks with ~1.5k-token JSON
    replies, and one reduce call over the map outputs plus signals.
    """
    corpus_tokens = post_count * 120
    map_chunks = max(1, round(corpus_tokens / 30_000))
    sonnet_in, sonnet_out = model_rates("claude-sonnet-5")
    opus_in, opus_out = model_rates("claude-opus-5")
    map_cost = (
        corpus_tokens * sonnet_in / 1_000_000
        + map_chunks * 1_500 * sonnet_out / 1_000_000
    )
    reduce_in = map_chunks * 1_500 + 3_000  # map outputs + signals.json
    reduce_cost = reduce_in * opus_in / 1_000_000 + 6_000 * opus_out / 1_000_000
    return map_cost + reduce_cost
