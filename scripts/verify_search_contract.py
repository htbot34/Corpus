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
— how many corroboration points each sat at, which negative fired. That is the
number that says whether the 2.0-point bar is calibrated or merely strict.

Read the census carefully, because one failure mode makes it meaningless:
**a candidate whose page was never read has never had its strong signals
looked at**, so a run that fetched nothing reports ~100% held for reasons that
have nothing to do with the threshold. The script guards that at both ends. It
probes reachability *before* spending anything, and it checks what the phase
actually did *afterwards* — because the pre-flight probe answers "can this
machine fetch a page", which is not the same question as "did this run read
one". On 2026-08-03 it printed a full census of a run that fetched zero of 50
candidates: egress was fine, and the phase had stopped before the verification
pass on evidence it did not have. A census this script knows to be an artifact
is now refused, with the reason.

NEVER wire this into CI. The offline suite is offline on purpose, and a check
that silently spends money on every push gets deleted after the first
surprising invoice. It refuses to run under CI unless forced.

Budget: one search per query, $0.01 each, plus a few cents of Haiku tokens.
At the default 12 queries that is ~$0.13. Page fetches are free.

Exit codes: 0 clean, 1 critical drift, 2 could not run — or ran and produced a
census it will not stand behind.
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
from corpus.search.scoring import CORROBORATION_THRESHOLD
from corpus.search.verify import DEFAULT_MAX_VERIFY_FETCHES, search_for_sources
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


def what_the_phase_did(result: Any) -> None:
    """Everything the phase recorded about its own run, before any counting.

    Printed unconditionally and first. The run this script exists to support
    stopped early, said so in `result.notes`, and nothing printed the notes —
    so the census below it read as a finding about the scorer.
    """
    _print("What the phase did")
    print(
        f"  {result.searches_run} search(es) ({result.cached_searches} cached), "
        f"{result.results_seen} result(s) seen"
    )
    print(
        f"  {result.reads_attempted} page read(s) attempted, {result.verified_count} succeeded "
        f"({result.fetches} request(s), {result.cached_fetches} from cache)"
    )
    if result.common_name:
        print("  the name was judged too common to resolve: nothing was ingested")
    for note in result.notes:
        print(f"  note: {note}")
    for problem in list(dict.fromkeys(result.errors))[:10]:
        print(f"  error: {problem}")


def artifact_reason(result: Any) -> str:
    """Why this run's census cannot answer the calibration question — "" if it can.

    The condition that matters is not "did anything get corroborated" — zero
    corroborated is a legitimate and interesting answer. It is whether the
    scorer was ever shown the evidence it scores on. Every strong signal lives
    in a fetched page, so a census taken over unread candidates measures the
    fetcher, and it measures it in a way that looks exactly like a threshold
    set too high.
    """
    if not result.everything:
        return ""
    if result.unread:
        return (
            f"{result.reads_attempted} candidate(s) needed their page read and "
            f"{result.verified_count} were read.\n"
            "  Every held candidate below was scored on a search result alone, so its "
            "strong\n  signals — author metadata, outbound links, the employer's name — "
            "were never\n  looked at. The outcome mix measures the fetcher, not the "
            "threshold, and it\n  looks identical to a scorer that is too strict."
        )
    if result.common_name:
        return (
            "the phase judged the name too common to resolve and demoted everything it "
            "found.\n  Nothing could reach `corroborated`, so the corroborated column is "
            "zero by\n  refusal rather than by scoring, and says nothing about where the "
            "threshold sits."
        )
    return ""


def refuse_census(result: Any, reason: str) -> None:
    """Say what happened and why the numbers are not the ones being asked for."""
    _print("Refusing to print a census")
    print(f"  {reason}\n")
    print("  What the run produced, which is not a calibration table:")
    for label, items in _buckets(result).items():
        print(f"    {label:<26} {len(items):>4}")
    print(
        "\n  To get a census that answers the threshold question, this run has to read\n"
        "  the candidate pages. Check the notes above for why it did not."
    )


def _buckets(result: Any) -> dict[str, Any]:
    return {
        "corroborated (ingested)": result.candidates,
        "held (unconfirmed.md)": result.held,
        "context (about them)": result.context,
        "rejected": result.rejected,
    }


def census(result: Any) -> None:
    """What the scorer decided, and why — the calibration question."""
    _print("Outcome census")
    buckets = _buckets(result)
    total = sum(len(v) for v in buckets.values()) or 1
    for label, items in buckets.items():
        print(f"  {label:<26} {len(items):>4}  ({len(items) / total:.0%})")
    print(f"  {'-' * 26} {'-' * 4}")
    print(f"  {'candidates seen':<26} {total:>4}")
    print(f"  {'verified (page read)':<26} {result.verified_count:>4}")

    if not result.held:
        return

    _print("Why the held ones were held")
    reasons: Counter[str] = Counter()
    point_counts: Counter[float] = Counter()
    for candidate in result.held:
        score = candidate.score
        if score is None:
            reasons["never scored"] += 1
            continue
        if not candidate.verified:
            reasons["never fetched (page unread)"] += 1
            continue
        point_counts[score.points] += 1
        if score.negatives:
            for negative in score.negatives:
                reasons[f"negative: {negative.name}"] += 1
        else:
            reasons[f"only {score.points:g} corroboration point(s)"] += 1
    for reason, count in reasons.most_common():
        print(f"  {count:>4}  {reason}")

    if point_counts:
        _print("Corroboration points among fetched-but-held candidates")
        print("  (this is the calibration table: how many sat just under the bar)")
        for pts in sorted(point_counts):
            print(f"  {point_counts[pts]:>4}  candidate(s) at {pts:g} point(s)")
        near = sum(c for pts, c in point_counts.items() if 0 < pts < CORROBORATION_THRESHOLD)
        fetched_held = sum(point_counts.values())
        if fetched_held and near / fetched_held > 0.5:
            print(
                f"\n  {near} of {fetched_held} fetched-and-held candidates carried real\n"
                f"  evidence and still sat under the {CORROBORATION_THRESHOLD:g}-point bar. If those are\n"
                f"  mostly the right person, the weights are too strict for real results\n"
                f"  and the fix is a signal that fires more often — not lowering the bar."
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
    # The default is imported rather than restated, so the checker and the
    # phase cannot quietly disagree about how many pages a run may read.
    parser.add_argument("--max-fetches", type=int, default=DEFAULT_MAX_VERIFY_FETCHES)
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
        print(f"  the contract records: {WEB_SEARCH.verified or 'nothing — never checked'}")
        print("\n  If that line is older than what you just ran, update it in")
        print("  corpus/search/contract.py, rebuild the fixture with")
        print("  tests/fixtures/_scrub_search.py, and the offline tests will keep both honest.")

    what_the_phase_did(result)
    reason = artifact_reason(result)
    if reason:
        refuse_census(result, reason)
    else:
        census(result)

    _print("Spend")
    for line in budget.summary_lines():
        print(f"  {line}")

    if capture is not None:
        print(f"\n  {capture.count} raw response(s) written to {capture.directory}")
        print("  Rebuild the fixture from one of these rather than from the docs.")

    cache.close()
    # Critical drift outranks a refused census: a renamed block is the thing
    # this script exists to catch, and it is actionable on its own.
    if worst_severity(violations) == CRITICAL:
        return 1
    return 2 if reason else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
