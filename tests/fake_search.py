"""A search provider that never leaves the process.

The counterpart to `fake_provider.py` and `fake_anthropic.py`. Tests that are
not *about* search still run through the phase now that it is on by default,
and they must not reach a vendor to do it — so they get this: a provider that
answers every query with nothing, reports no usage, and therefore costs
nothing.

Reporting zero usage is the part that matters. `SearchClient` bills from
`last_usage`, so a fake that claimed a search would quietly move every spend
assertion in the suite.
"""

from __future__ import annotations

from corpus.search.providers import SearchResult, SearchUsage


class SilentSearchProvider:
    """Answers every query with nothing, and bills for nothing."""

    name = "silent"

    def __init__(self, results: dict[str, list[SearchResult]] | None = None) -> None:
        self.results = results or {}
        self.queries: list[str] = []
        self.model = "claude-haiku-4-5-20251001"
        self.last_usage = SearchUsage(model=self.model)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        # No searches, no tokens: a test that did not ask to spend money does
        # not spend any.
        self.last_usage = SearchUsage(model=self.model)
        return list(self.results.get(query, []))[:limit]

    def close(self) -> None:
        return None
