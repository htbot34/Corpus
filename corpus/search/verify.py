"""The two passes: discover, then verify.

A search snippet is not evidence. It is 150 characters the model chose to
quote, on a page nobody has read, and every strong signal in `scoring.py` —
author metadata, outbound links to an anchor, the employer's name — lives in
the page rather than the snippet. Scoring a snippet and ingesting on the
result would be a tool that trusts search engines to do attribution.

So:

1. **Discover.** Run the queries. Every hit becomes a candidate with the query
   that found it. Score against the snippet, which is only ever used to decide
   whether a page is worth fetching, and to throw out the ones that are
   disqualified on their URL alone.
2. **Verify.** Fetch the survivors and score against the *fetched page*. Only
   that score can promote anything.

Every judgement that needs to know what a page says is therefore made after
pass 2, including the common-name refusal. A gate placed between the two passes
can only see snippets, and a gate that stops the run can only be wrong in one
direction — see `detect_common_name` for the live run that proved it.

`attribution_basis` records which pass a document's verdict came from, so the
report can say *why* a document was trusted rather than only how much.

Nothing here promotes to `linked`, and nothing here ingests a `name_match`.
Those two facts are what make the phase safe to run at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..budget import BudgetExceeded
from ..cache import Cache
from ..discovery import Fetcher, kind_for, normalize_url
from ..identity import IdentityCard
from .client import SearchClient
from .pagefacts import extract_facts, facts_from_snippet
from .queries import DEFAULT_MAX_SEARCHES, Query, generate_queries, missing_fields
from .scoring import CandidateScore, score_candidate

#: Results kept per query. Past the first page or so, relevance falls off a
#: cliff and the odds of a same-name stranger climb, so a bigger number buys
#: noise rather than coverage.
RESULTS_PER_QUERY = 8

#: Pages fetched in the verification pass. Plain HTTP against public pages, so
#: this is not a money guard — it is a guard against a hundred requests to
#: other people's servers on behalf of one search run. 40 rather than 20
#: because the dustinw run needed 27 page reads and the old cap left 14 of
#: its 24 held candidates unread: a ceiling that stops the pass from reading
#: pages it already decided to read manufactures holds out of nothing.
DEFAULT_MAX_VERIFY_FETCHES = 40

#: The negatives that count as identity contradictions: a fetched page
#: attaching a different employer, a different professional field, or a
#: different location to the target's name.
CONFLICT_NEGATIVES = ("different_employer", "different_field", "different_location")

#: Distinct hosts carrying conflicting identity facts before the name is
#: treated as ambiguous. Two independent pages putting the same name at two
#: different employers is a collision; forty domains all putting it at the
#: same one is fame.
COMMON_NAME_CONFLICT_HOSTS = 2


@dataclass
class SearchCandidate:
    """One URL search proposed, and everything decided about it."""

    url: str
    title: str = ""
    snippet: str = ""
    query: str = ""
    published_at: datetime | None = None
    kind: str = "web"
    #: The verdict from the pass that last scored it.
    score: CandidateScore | None = None
    verified: bool = False
    #: Why it was never fetched, when it was not.
    skipped: str = ""

    @property
    def outcome(self) -> str:
        return self.score.outcome if self.score is not None else "held"

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower().removeprefix("www.")

    @property
    def ingestible(self) -> bool:
        """Corroborated, verified, and readable by an adapter we have."""
        return (
            self.score is not None
            and self.score.ingestible
            and self.verified
            and self.kind
            in ("rss", "substack", "web", "github", "bluesky", "hn", "reddit", "mastodon")
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "query": self.query,
            "kind": self.kind,
            "verified": self.verified,
        }
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.score is not None:
            payload.update(self.score.as_dict())
        return payload


@dataclass
class SearchPhaseResult:
    """What Phase 2 found, what it held, and what it refused."""

    queries: list[Query] = field(default_factory=list)
    #: Corroborated and verified. These are ingested.
    candidates: list[SearchCandidate] = field(default_factory=list)
    #: name_match. Written to unconfirmed.md; never ingested without a human.
    held: list[SearchCandidate] = field(default_factory=list)
    #: About them rather than by them. Recorded, never a corpus document.
    context: list[SearchCandidate] = field(default_factory=list)
    #: Excluded, aggregator, people-search, citation-only.
    rejected: list[SearchCandidate] = field(default_factory=list)
    #: Facts that corroborate the card without being sources.
    identity_signals: list[str] = field(default_factory=list)
    searches_run: int = 0
    cached_searches: int = 0
    results_seen: int = 0
    fetches: int = 0
    cached_fetches: int = 0
    #: Candidates the verification pass tried to read a page for. Rejections on
    #: the URL alone never reach it, so this is the denominator that says
    #: whether verification ran at all — `fetches` cannot, because a cache hit
    #: is a read that costs no request.
    reads_attempted: int = 0
    #: The fetch ceiling this run was given, so the report can say what bound.
    max_fetches: int = 0
    #: True when independent pages attach conflicting identity facts to the
    #: name. Nothing is ingested.
    common_name: bool = False
    #: Card fields that would most improve the next run.
    disambiguators: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(1 for c in self.everything if c.verified)

    @property
    def unread(self) -> bool:
        """Did the phase score candidates without reading a single page?

        The one condition that makes every number below it meaningless. Strong
        signals live in fetched pages, so a run that read nothing scores every
        candidate on the search result alone and holds all of them — which is
        indistinguishable, from the outside, from a scorer whose threshold is
        too strict. Anything reporting on this phase has to check it before
        drawing a conclusion from the outcome mix.
        """
        return self.reads_attempted > 0 and self.verified_count == 0

    @property
    def everything(self) -> list[SearchCandidate]:
        return [*self.candidates, *self.held, *self.context, *self.rejected]

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries": [{"text": q.text, "why": q.why} for q in self.queries],
            "searches": self.searches_run,
            "cached_searches": self.cached_searches,
            "results_seen": self.results_seen,
            "fetches": self.fetches,
            "reads_attempted": self.reads_attempted,
            "max_fetches": self.max_fetches,
            "verified": self.verified_count,
            "unread": self.unread,
            "common_name": self.common_name,
            "disambiguators": list(self.disambiguators),
            "candidates": [c.as_dict() for c in self.candidates],
            "held": [c.as_dict() for c in self.held],
            "context": [c.as_dict() for c in self.context],
            "rejected": [c.as_dict() for c in self.rejected],
            "identity_signals": list(self.identity_signals),
            "notes": list(self.notes),
            "errors": list(self.errors),
        }


def snippet_promise(score: CandidateScore) -> tuple[float, bool]:
    """How promising a snippet looks, for deciding fetch *order*.

    Deliberately not a floor. An earlier version refused to fetch a candidate
    whose snippet carried neither the name nor a signal, and that silently
    dropped correct sources: a search result is 150 characters a model chose
    to quote, and a page can be unmistakably theirs — declared author,
    employer named, linking to their GitHub — while its snippet mentions none
    of it. The query already connected the result to the target; requiring the
    snippet to say so again bought no attribution and cost real coverage.

    So every candidate that was not rejected outright gets fetched, and this
    only decides who goes first when `--max-verify-fetches` binds. Ordered by
    corroboration points rather than a signal count, for the same reason the
    threshold is: a snippet carrying one strong signal is a better bet than
    one carrying one moderate.
    """
    return score.points, score.name_present


def detect_common_name(scores: list[CandidateScore]) -> tuple[bool, list[str]]:
    """Does the candidate set carry mutually contradictory identity claims?

    This used to count distinct domains that matched the name and nothing
    else, and that inference is backwards for a public figure: many domains
    mentioning one name is evidence of reach, not of several people sharing
    it. It fired on Simon Willison — 54 results, 25 pages read, every
    candidate silently held — and the better known the target, the more
    certainly it misfired. Domain diversity is what search looking *well*
    looks like.

    Ambiguity is a different signal: independent pages attaching
    *conflicting* identity facts to the same name — different employers,
    different fields, different locations — which are exactly the negatives
    `scoring.py` already computes against the card. Two independent pages
    putting the same name at two different employers is a collision; this
    fires on that and on nothing else.

    **Only fetched scores are counted, and that is still load-bearing**: a
    snippet can neither confirm nor contradict anything (see the 2026-08-03
    run, where empty vendor snippets made 50 candidates look like eight
    people). And where the card supplies no employer, role or location,
    contradiction cannot be established at all — the caller declines to run
    this check and says so, rather than falling back to counting domains.

    Returns (fired, conflict descriptions). The descriptions name the actual
    conflict per host, because "the name is too common" is not a finding;
    "two pages put this name at different employers" is.
    """
    conflicts_by_host: dict[str, str] = {}
    for score in scores:
        if not score.fetched or not score.name_present or score.outcome == "rejected":
            continue
        host = (urlparse(score.url).hostname or "").lower().removeprefix("www.")
        if not host:
            continue
        for negative in score.negatives:
            if negative.name in CONFLICT_NEGATIVES:
                conflicts_by_host.setdefault(host, f"{host}: {negative.detail}")
                break
    if len(conflicts_by_host) < COMMON_NAME_CONFLICT_HOSTS:
        return False, []
    return True, [conflicts_by_host[host] for host in sorted(conflicts_by_host)]


def search_for_sources(
    card: IdentityCard,
    cache: Cache,
    client: SearchClient,
    *,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    max_fetches: int = DEFAULT_MAX_VERIFY_FETCHES,
    known_urls: set[str] | None = None,
    log: Callable[[str], None] = print,
) -> SearchPhaseResult:
    """Run Phase 2 end to end. Never raises; degrades and reports."""
    result = SearchPhaseResult(queries=generate_queries(card, max_searches))
    result.max_fetches = max_fetches
    known = {normalize_url(u) for u in (known_urls or set())}
    known.discard("")

    if not result.queries:
        result.notes.append(
            "no searchable query could be built from this card — a name plus at least "
            "one of employer, role, or an anchor handle is the minimum"
        )
        return result

    # ---- pass 1: discover -------------------------------------------------
    candidates: dict[str, SearchCandidate] = {}
    for query in result.queries:
        try:
            hits = client.search(query.text, RESULTS_PER_QUERY)
        except BudgetExceeded as exc:
            # The budget refusing a query stops the phase, but it must not
            # throw away the queries already paid for. Verification below runs
            # on what we have, and the report says the phase was cut short.
            result.notes.append(f"search stopped early: {exc}")
            break
        for hit in hits:
            result.results_seen += 1
            url = normalize_url(hit.url)
            if not url or url in candidates:
                continue
            if url in known:
                # Phase 1 already has it, with better attribution than search
                # could ever assign. Finding it again is corroboration, not a
                # new source.
                result.identity_signals.append(f"search independently found {url}, already known")
                continue
            candidates[url] = SearchCandidate(
                url=url,
                title=hit.title,
                snippet=hit.snippet,
                query=query.text,
                published_at=hit.published_at,
                kind=kind_for(url),
            )
    result.searches_run = client.searches_run
    result.cached_searches = client.cached_searches
    result.errors.extend(client.errors)

    for candidate in candidates.values():
        facts = facts_from_snippet(candidate.url, candidate.title, candidate.snippet)
        candidate.score = score_candidate(facts, card, query=candidate.query)

    # ---- pass 2: verify ---------------------------------------------------
    # Ordered by how promising the snippet was, so a low fetch cap spends its
    # requests on the candidates most likely to survive.
    ranked = sorted(
        candidates.values(),
        key=lambda c: (
            c.score.outcome == "rejected" if c.score else True,
            -snippet_promise(c.score)[0] if c.score else 0,
            not (snippet_promise(c.score)[1] if c.score else False),
            c.url,
        ),
    )

    fetcher = Fetcher(cache, max_fetches, log)
    try:
        for candidate in ranked:
            score = candidate.score
            if score is not None and score.outcome == "rejected":
                # Disqualified by its URL alone. No request needed, and making
                # one would be a request to a data broker.
                candidate.skipped = "rejected before fetching"
                _file(result, candidate)
                continue
            result.reads_attempted += 1
            body = fetcher.get(candidate.url)
            if body is None:
                # A dead host, or the fetch cap. Either way the page was never
                # read, so it is held on the snippet alone — which can never
                # be enough.
                candidate.skipped = "could not be fetched; held on the snippet alone"
                _file(result, candidate)
                continue

            page = extract_facts(body, candidate.url)
            candidate.score = score_candidate(page, card, query=candidate.query)
            candidate.verified = True
            _file(result, candidate)
    finally:
        fetcher.close()

    result.fetches = fetcher.fetches
    result.cached_fetches = fetcher.cached
    result.errors.extend(e for e in dict.fromkeys(fetcher.errors) if e not in result.errors)

    # ---- the stop-and-ask path -------------------------------------------
    # Deliberately after the fetches rather than before them. A name is
    # ambiguous when independent *pages* attach conflicting identity facts to
    # it, and the only way to know that is to have read them — see
    # `detect_common_name`. Asking first, on snippets, is what stopped a live
    # run before it read anything.
    #
    # The cost of asking late is bounded by `max_fetches`: plain HTTP against
    # public pages, capped, cached, and free. The cost of asking early was a
    # whole phase that never ran.
    _stop_if_common_name(result, card, log)

    if result.unread:
        result.notes.append(
            "no candidate page could be read, so every verdict here rests on a search "
            "result alone. That is a fetch failure rather than a finding about the "
            "subject: the signals that promote a candidate live in the page."
        )

    for bucket in (result.candidates, result.held, result.context, result.rejected):
        bucket.sort(key=lambda c: c.url)
    result.notes = list(dict.fromkeys(result.notes))
    return result


def _stop_if_common_name(
    result: SearchPhaseResult,
    card: IdentityCard,
    log: Callable[[str], None],
) -> None:
    """Demote everything if the pages say this name belongs to several people.

    Demotion rather than an early return, because by this point the pages have
    been read and throwing that away would mean re-fetching them to answer the
    same question next run. Nothing is ingested either way — which is the whole
    guarantee — but the evidence for the refusal is now in `discovery.json`
    instead of implied by its absence.
    """
    fetched = [c.score for c in result.everything if c.score is not None and c.score.fetched]
    if not (card.employer or card.role or card.location):
        # Contradiction needs something to contradict. With none of the
        # fields a conflict could be read against, this check declines to
        # run and says so — the old fallback of counting domains fired
        # hardest exactly where search works best, on well-known names.
        if any(s.name_present for s in fetched):
            result.notes.append(
                "the name-collision check did not run: the card names no employer, "
                "role or location for a page to contradict, so ambiguity cannot be "
                "told apart from reach. Adding one of those fields enables it."
            )
        return
    common, conflicts = detect_common_name(fetched)
    if not common:
        return

    result.common_name = True
    result.disambiguators = missing_fields(card)
    shown = "; ".join(conflicts[:4]) + ("; …" if len(conflicts) > 4 else "")
    note = (
        f"{len(conflicts)} independent page(s) attach conflicting identity facts to "
        f"this name: {shown}. Continuing would mean guessing which of these people "
        f"is the subject, so nothing from search has been ingested."
    )
    result.notes.append(note)
    # Logged, not merely recorded. A phase that refuses to ingest anything must
    # say so where the run is being watched; `scripts/verify_search_contract.py`
    # printed a census of this exact outcome without ever mentioning it.
    log(f"  [search] {note}")

    for candidate in result.candidates:
        candidate.skipped = "held: independent pages attach conflicting identity facts to this name"
    result.held.extend(result.candidates)
    result.candidates = []


def _file(result: SearchPhaseResult, candidate: SearchCandidate) -> None:
    """Put a scored candidate in the one bucket its verdict allows."""
    outcome = candidate.outcome
    if outcome == "rejected":
        result.rejected.append(candidate)
    elif outcome == "context":
        result.context.append(candidate)
    elif outcome == "corroborated" and candidate.verified:
        result.candidates.append(candidate)
    else:
        # Corroborated on a snippet is not corroborated. Without a fetched page
        # the strong signals were never checked, so it waits for a human.
        if outcome == "corroborated":
            candidate.skipped = candidate.skipped or "scored on the snippet only, never verified"
        result.held.append(candidate)
