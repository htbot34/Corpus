"""Caching, billing wrapper around a SearchProvider.

The same seam `XClient` is: providers fetch, the client pays and remembers.
Nothing above this file knows which vendor ran the query, and nothing below it
knows there is a budget.

Two guarantees live here, and both matter more for search than they did for X:

* **Every search is reserved before it is made.** A search is $0.01 of fee plus
  tokens — small individually, and 12 of them is a real fraction of a
  `--budget 1.50` run. `Budget.reserve()` refuses the call it cannot cover
  rather than making it and reporting the overshoot afterwards.
* **Results are cached by query string, permanently.** Search results for
  "Jane Smith Acme Corp" do not change usefully between a run and the re-run
  ten minutes later where you fixed the scoring. Iterating on Phase 2 must not
  cost a dollar a lap, so it costs nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..budget import SEARCH_COST_PER_QUERY, Budget, estimate_search_call
from ..cache import Cache
from .providers import (
    SEARCH_MAX_TOKENS,
    SearchError,
    SearchProvider,
    SearchResult,
    SearchUsage,
    SimilarSearchProvider,
)

# Input tokens for one search call: the system prompt, the query, and the
# results the tool feeds back in. Biased high, like every other estimator here —
# an under-estimate is what lets an overshoot through.
ASSUMED_SEARCH_INPUT_TOKENS = 4_000


class SearchClient:
    """Runs queries through a provider, cache-first, budget-checked."""

    def __init__(
        self,
        provider: SearchProvider,
        cache: Cache,
        budget: Budget,
        log: Callable[[str], None] = print,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.budget = budget
        self.log = log
        #: Queries actually sent to the provider, and queries served from cache.
        self.searches_run = 0
        self.cached_searches = 0
        self.errors: list[str] = []

    def _cache_key(self, query: str) -> str:
        # Keyed by provider too: two vendors answering the same query are two
        # different answers, and serving one from the other's cache would make
        # a provider swap invisible.
        return f"query:{self.provider.name}:{query}"

    def _search_rate(self) -> float:
        """The provider's worst-case per-search rate, for reserving and billing.

        Read with a default rather than required, so a provider written before
        rates were per-provider (every test fake, any third-party one) keeps
        billing at the Anthropic rate it always billed at.
        """
        return float(getattr(self.provider, "cost_per_search", SEARCH_COST_PER_QUERY))

    @property
    def finds_similar(self) -> bool:
        """Can this provider be asked for "more pages like this one"?

        Both halves checked — the declared flag and the method — so a provider
        lacking either is simply a provider without the capability, never a
        broken one.
        """
        return bool(getattr(self.provider, "supports_find_similar", False)) and isinstance(
            self.provider, SimilarSearchProvider
        )

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """One query, cache-first. Returns [] rather than raising."""
        return self._run(
            cache_key=self._cache_key(query),
            label=query,
            call=lambda p: p.search(query, limit),
            limit=limit,
        )

    def find_similar(self, url: str, limit: int = 10) -> list[SearchResult]:
        """Pages like the one at `url`, cache-first. Returns [] rather than raising.

        For a provider without the capability this is a quiet no-op — no
        reservation, no charge, no error — because "this vendor cannot do
        that" is a fact about the vendor, not a failure of the run.
        """
        if not self.finds_similar:
            return []
        provider = self.provider
        assert isinstance(provider, SimilarSearchProvider)  # narrowed by finds_similar
        return self._run(
            cache_key=f"similar:{provider.name}:{url}",
            label=f"more like {url}",
            call=lambda p: p.find_similar(url, limit),
            limit=limit,
        )

    def _run(
        self,
        *,
        cache_key: str,
        label: str,
        call: Callable[[Any], list[SearchResult]],
        limit: int,
    ) -> list[SearchResult]:
        """The one path both kinds of query take: cache, reserve, call, bill."""
        cached = self.cache.get("search", cache_key)
        if cached is not None:
            self.cached_searches += 1
            return [SearchResult.model_validate(r) for r in cached][:limit]

        if self.cache.offline:
            self.errors.append(f"--offline: {label!r} is not cached")
            return []

        estimate = estimate_search_call(
            getattr(self.provider, "model", ""),
            ASSUMED_SEARCH_INPUT_TOKENS,
            SEARCH_MAX_TOKENS,
            searches=1,
            cost_per_search=self._search_rate(),
        )
        # Deliberately not caught: a budget that refuses a search must stop the
        # phase, not be logged as a search that found nothing.
        with self.budget.reserved_for(estimate, f"search: {label[:60]}") as reservation:
            try:
                results = call(self.provider)
            except SearchError as exc:
                # One dead query must not end the phase; the run degrades and
                # says so, like every other adapter in this tool.
                self.errors.append(str(exc))
                self.log(f"  search failed (non-fatal): {exc}")
                return []
            usage: SearchUsage = getattr(self.provider, "last_usage", SearchUsage())
            reservation.actual = self._charge(label, usage)

        self.searches_run += 1
        self.errors.extend(f"{label!r}: provider error {code}" for code in usage.errors)
        if results or not usage.errors:
            # Permanent: a query's results are worth re-reading for free
            # forever, and the alternative is paying $0.01 to learn the same
            # thing again. An empty list with no errors is cached too — a
            # search that matched nothing is a finding.
            self.cache.put(
                "search",
                cache_key,
                [r.model_dump(mode="json") for r in results],
                permanent=True,
            )
        else:
            # Empty AND errored is not a finding — it is a provider answering
            # 200 with a shape the parser could not read, or an in-band error
            # where the results belong. Caching that permanently would turn
            # one bad response into "this person has no web presence" on
            # every future run, silently. The query is billed (it happened)
            # and will run again next time.
            self.log(f"  [search] {label!r}: errored with no results — not cached")
        return results

    def _charge(self, query: str, usage: SearchUsage) -> float:
        """Bill what happened: the searches performed, then the tokens.

        Charged separately because they are separate meters on the invoice, and
        a run that spent $0.12 on search fees and $0.004 on tokens should not
        report one number that explains neither.

        The rate is the provider's own, and when the provider's response
        states what the call actually cost (Exa's does), that figure wins —
        the invoice's number always beats our copy of the price list.
        """
        note = query[:80]
        cost = 0.0
        if usage.searches:
            unit_cost = (
                usage.cost_dollars / usage.searches
                if usage.cost_dollars is not None
                else self._search_rate()
            )
            cost += self.budget.charge_searches(
                "web_search", usage.searches, note=note, unit_cost=unit_cost
            ).cost
        if usage.model and (usage.input_tokens or usage.output_tokens):
            cost += self.budget.charge_anthropic(
                usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                note=f"search: {note}",
            )
        return cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "searches": self.searches_run,
            "cached_searches": self.cached_searches,
            "provider": self.provider.name,
            "errors": list(dict.fromkeys(self.errors)),
        }

    def close(self) -> None:
        self.provider.close()
