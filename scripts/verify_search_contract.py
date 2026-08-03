#!/usr/bin/env python3
"""Live search drift check, and a census of what the scorer actually does.

    python scripts/verify_search_contract.py --target janesmith
    python scripts/verify_search_contract.py --target janesmith --capture-search captures/
    python scripts/verify_search_contract.py --target janesmith --dry-run   # free

This is the online half of `corpus/search/contract.py`.
`tests/test_search_wire_contract.py` checks the fixture against that spec on
every test run; this checks the *API* against the same spec, so the two cannot
drift apart without something going red.

It answers two questions that cannot be answered offline:

**1. Does `web_search_20250305` return the shape the fixture assumes?** Every
response is checked field by field against the contract and the violations are
printed with their severity. `--capture-search` writes the raw responses so the
fixture can be rebuilt from evidence instead of documentation.

**2. How many candidates actually come back corroborated versus held?** The
census at the end breaks the outcomes down and, for the held ones, counts *why*
— which signal was missing, which negative fired. That is the number that says
whether the two-signal threshold is calibrated or merely strict.

Read the census carefully, because one failure mode makes it meaningless:
**if page fetches are blocked, everything is held regardless of the
threshold.** A candidate that cannot be fetched has never had its strong
signals looked at, so a run behind a restrictive egress policy reports 100%
held for reasons that have nothing to do with scoring. The script checks
reachability first and refuses to print a census it knows is an artifact.

NEVER wire this into CI. The offline suite is offline on purpose, and a check
that silently spends money on every push gets deleted after the first
surprising invoice. It refuses to run under CI unless forced.

Budget: one search per query, $0.01 each, plus a few cents of Haiku tokens.
At the default 12 queries that is ~$0.13. Page fetches are free.

Exit codes: 0 clean, 1 critical drift, 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.budget import SEARCH_COST_PER_QUERY, Budget
from corpus.cache import Cache
from corpus.identity import IdentityError, load_target
from corpus.search.capture import SearchCapture
from corpus.search.client import SearchClient
from corpus.search.contract import WEB_SEARCH, check_search_response
from corpus.search.providers import SearchError, get_search_provider
from corpus.search.queries import generate_queries
from corpus.search.verify import search_for_sources
from corpus.sources.base import http_client
from corpus.x.contract import CRITICAL, Violation, worst_severity

# A host that is certain to exist and is not the API, used only to tell
# "this person has no web presence" apart from "this network cannot fetch
# pages". Without that distinction the census below is unreadable.
REACHABILITY_PROBE = "https://example.com"


def _print(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def check_reachability() -> str:
    """Can this environment fetch a page at all? Returns "" when it can."""
    client = http_client(timeout=15.0)
    try:
        client.get(REACHABILITY_PROBE)
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        client.close()


def census(result: Any) -> None:
    """What the scorer decided, and why — the calibration question."""
    _print("Outcome census")
    buckets = {
        "corroborated (ingested)": result.candidates,
        "held (unconfirmed.md)": result.held,
        "context (about them)": result.context,
        "rejected": result.rejected,
    }
    total = sum(len(v) for v in buckets.values()) or 1
    for label, items in buckets.items():
        print(f"  {label:<26} {len(items):>4}  ({len(items) / total:.0%})")
    print(f"  {'-' * 26} {'-' * 4}")
    print(f"  {'candidates seen':<26} {total:>4}")
    print(f"  {'verified (page fetched)':<26} {result.verified_count:>4}")

    if not result.held:
        return

    _print("Why the held ones were held")
    reasons: Counter[str] = Counter()
    signal_counts: Counter[int] = Counter()
    for candidate in result.held:
        score = candidate.score
        if score is None:
            reasons["never scored"] += 1
            continue
        if not candidate.verified:
            reasons["never fetched (page unread)"] += 1
            continue
        signal_counts[score.independent_count] += 1
        if score.negatives:
            for negative in score.negatives:
                reasons[f"negative: {negative.name}"] += 1
        else:
            reasons[f"only {score.independent_count} independent signal(s)"] += 1
    for reason, count in reasons.most_common():
        print(f"  {count:>4}  {reason}")

    if signal_counts:
        _print("Signal counts among fetched-but-held candidates")
        print("  (this is the calibration table: how many sat at exactly one)")
        for n in sorted(signal_counts):
            print(f"  {signal_counts[n]:>4}  candidate(s) with {n} independent signal(s)")
        one = signal_counts.get(1, 0)
        fetched_held = sum(signal_counts.values())
        if fetched_held and one / fetched_held > 0.5:
            print(
                f"\n  {one} of {fetched_held} fetched-and-held candidates had exactly one\n"
                f"  signal. If those are mostly the right person, the threshold of two is\n"
                f"  too strict for real results and the fix is a third weak signal that\n"
                f"  fires more often — not lowering the bar to one."
            )

    _print("Which signals actually fired, across every scored candidate")
    fired: Counter[str] = Counter()
    for candidate in result.everything:
        if candidate.score is None:
            continue
        for signal in candidate.score.signals:
            fired[f"{signal.name} ({signal.weight})"] += 1
    for signal, count in fired.most_common():
        print(f"  {count:>4}  {signal}")
    if not fired:
        print("  none — no candidate produced a single signal")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="a saved target from profiles.yaml")
    parser.add_argument("--max-searches", type=int, default=12)
    parser.add_argument("--max-fetches", type=int, default=20)
    parser.add_argument("--budget", type=float, default=1.00)
    parser.add_argument("--capture-search", metavar="DIR", default=None)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and price, no calls")
    parser.add_argument("--force", action="store_true", help="run even under CI")
    args = parser.parse_args(argv[1:])

    if os.environ.get("CI") and not args.force:
        print("refusing to run under CI: this spends money. Pass --force to override.")
        return 2

    try:
        card = load_target(args.target)
    except IdentityError as exc:
        print(f"could not load target: {exc}")
        return 2

    queries = generate_queries(card, args.max_searches)
    _print(f"Plan for {card.display}")
    for query in queries:
        print(f"  {query.text}   ({query.why})")
    price = len(queries) * SEARCH_COST_PER_QUERY
    print(f"\n  {len(queries)} search(es) x ${SEARCH_COST_PER_QUERY:.3f} = ~${price:.3f} in fees")
    print("  plus a few cents of Haiku tokens. Page fetches are free.")

    if args.dry_run:
        print("\n--dry-run: nothing was called.")
        return 0

    # Order matters. A blocked egress policy makes the census meaningless, and
    # finding that out *after* spending $0.13 would be an expensive way to
    # learn it.
    unreachable = check_reachability()
    if unreachable:
        _print("Refusing to run")
        print(
            f"  cannot fetch {REACHABILITY_PROBE}: {unreachable}\n\n"
            "  The verification pass fetches every candidate page, and scoring a page\n"
            "  nobody can read produces zero strong signals. This run would report\n"
            "  ~100% held for reasons that have nothing to do with the threshold,\n"
            "  which is worse than no data: it looks exactly like evidence that the\n"
            "  scorer is too strict.\n\n"
            "  Unblock outbound HTTPS to candidate hosts and run this again."
        )
        return 2

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("\nANTHROPIC_API_KEY is not set, so no search can be made.")
        return 2

    cache = Cache()
    budget = Budget(limit=args.budget)
    capture = SearchCapture(args.capture_search) if args.capture_search else None
    responses: list[dict[str, Any]] = []

    class Recording:
        """Wraps the provider so every raw response reaches the contract check."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.name = inner.name
            self.model = getattr(inner, "model", "")

        @property
        def last_usage(self) -> Any:
            return self.inner.last_usage

        def search(self, query: str, limit: int) -> Any:
            results = self.inner.search(query, limit)
            raw = getattr(self.inner, "last_raw_message", None)
            if raw is not None:
                responses.append(raw)
            return results

        def close(self) -> None:
            self.inner.close()

    try:
        provider = get_search_provider(capture=capture, log=print)
    except (SearchError, NotImplementedError) as exc:
        print(f"\ncould not build a search provider: {exc}")
        return 2

    client = SearchClient(Recording(provider), cache, budget, log=print)
    _print("Running")
    try:
        result = search_for_sources(
            card,
            cache,
            client,
            max_searches=args.max_searches,
            max_fetches=args.max_fetches,
            log=print,
        )
    finally:
        client.close()

    _print("Contract check")
    violations: list[Violation] = []
    for payload in responses:
        violations.extend(check_search_response(payload))
    if not responses:
        print(
            "  no raw responses were recorded. The provider does not expose\n"
            "  `last_raw_message`; use --capture-search and check the files."
        )
    elif violations:
        for violation in violations:
            print(f"  {violation}")
    else:
        print(f"  clean: {len(responses)} response(s) match {WEB_SEARCH.name}")
        print("\n  Now record it: set WEB_SEARCH.verified in corpus/search/contract.py,")
        print("  update the fixture's _provenance, and the offline test will remind you.")

    census(result)

    _print("Spend")
    for line in budget.summary_lines():
        print(f"  {line}")

    if capture is not None:
        print(f"\n  {capture.count} raw response(s) written to {capture.directory}")
        print("  Rebuild the fixture from one of these rather than from the docs.")

    cache.close()
    return 1 if worst_severity(violations) == CRITICAL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
