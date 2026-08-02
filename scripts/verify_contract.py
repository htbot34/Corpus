#!/usr/bin/env python3
"""Live provider drift check. Costs about a cent. Run it monthly.

    python scripts/verify_contract.py
    python scripts/verify_contract.py --handle pmarca --capture-raw captures/
    python scripts/verify_contract.py --dry-run      # plan and price, no calls

This is the online half of the wire contract. `tests/test_wire_contract.py`
checks the fixtures against `corpus/x/contract.py` on every test run; this
checks the *provider* against the same spec, so the two cannot drift apart
without something going red.

NEVER wire this into CI. The offline suite is offline on purpose, and a check
that silently spends money on every push is a check that gets deleted after the
first surprising invoice. It refuses to run under CI unless forced.

Budget: one profile read plus about sixty tweet reads.

    profile           $0.00018
    last_tweets       ~20 tweets  $0.0030
    advanced_search   ~20 tweets  $0.0030
    tweets_by_ids     ~5 tweets   $0.0008
                                  --------
                                  ~$0.007

Exit codes: 0 clean, 1 critical drift, 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.budget import X_COST_PER_PROFILE, X_COST_PER_TWEET
from corpus.x.capture import RawCapture
from corpus.x.client import _tweet_id, parse_created_at
from corpus.x.contract import (
    ADVANCED_SEARCH,
    CRITICAL,
    IMPORTANT,
    LAST_TWEETS,
    TWEETS_BY_IDS,
    USER_INFO,
    Violation,
    check_payload,
    locate_items,
)
from corpus.x.providers import ProviderError, TwitterApiIoProvider

try:  # dotenv is a declared dependency, but this script must work standalone.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# A high-volume account with a long, continuous history. The point is a target
# whose shape exercises replies, quotes, and deep history — not this particular
# person.
DEFAULT_HANDLE = "paulg"

BOLD = "\033[1m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
YELLOW = "\033[33m" if sys.stdout.isatty() else ""
GREEN = "\033[32m" if sys.stdout.isatty() else ""
OFF = "\033[0m" if sys.stdout.isatty() else ""


class Report:
    def __init__(self) -> None:
        self.violations: list[Violation] = []
        self.findings: list[str] = []
        self.tweet_reads = 0
        self.profile_reads = 0

    @property
    def spend(self) -> float:
        return self.tweet_reads * X_COST_PER_TWEET + self.profile_reads * X_COST_PER_PROFILE

    def add(self, violations: list[Violation]) -> None:
        self.violations.extend(violations)

    def finding(self, text: str) -> None:
        self.findings.append(text)
        print(f"    {YELLOW}? {text}{OFF}")

    def ok(self, text: str) -> None:
        print(f"    {GREEN}+{OFF} {text}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")


def show(violations: list[Violation]) -> None:
    for v in violations:
        colour = RED if v.severity == CRITICAL else YELLOW if v.severity == IMPORTANT else ""
        print(f"    {colour}[{v.severity}]{OFF} {v.message}")
    if not violations:
        print(f"    {GREEN}+{OFF} matches contract")


# --------------------------------------------------------------------------


def check_user_info(provider: TwitterApiIoProvider, handle: str, report: Report) -> dict[str, Any]:
    section(f"1. user_info  GET {USER_INFO.path}")
    payload = provider._get(USER_INFO.path, {"userName": handle})
    report.profile_reads += 1
    show(check_payload(USER_INFO, payload))

    data = payload.get("data") or {}
    if "msg" not in payload:
        report.finding("envelope has no 'msg' — confirmed present on 2026-07-31")
    created = data.get("createdAt", "")
    if created and not (created.endswith("Z") and "." in created):
        report.finding(
            f"createdAt format changed: {created!r} (expected ISO-8601 with "
            "six-digit microseconds and a Z suffix)"
        )
    else:
        report.ok(f"createdAt format holds: {created!r}")
    if not isinstance(data.get("statusesCount"), int):
        report.finding("statusesCount missing or not an int — Phase 2.3 depends on it")
    else:
        report.ok(f"statusesCount = {data['statusesCount']:,}")
    return data


def check_last_tweets(
    provider: TwitterApiIoProvider, handle: str, report: Report
) -> list[dict[str, Any]]:
    section(f"2. last_tweets  GET {LAST_TWEETS.path}")
    payload = provider._get(LAST_TWEETS.path, {"userName": handle})
    location, tweets = locate_items(LAST_TWEETS, payload)
    report.tweet_reads += len(tweets)
    print(f"    array at {location!r}, {len(tweets)} tweets")
    print(f"    top-level keys: {sorted(payload)}")
    show(check_payload(LAST_TWEETS, payload))
    _report_timestamp_format(tweets, report)
    return tweets


def check_advanced_search(
    provider: TwitterApiIoProvider, handle: str, report: Report
) -> list[dict[str, Any]]:
    section(f"3. advanced_search  GET {ADVANCED_SEARCH.path}")

    # A 30-day window ending yesterday: recent enough to be populated, bounded
    # enough that honouring the operators is testable.
    until = datetime.now(tz=timezone.utc) - timedelta(days=1)
    since = until - timedelta(days=30)
    since_ts, until_ts = int(since.timestamp()), int(until.timestamp())
    query = f"from:{handle} since_time:{since_ts} until_time:{until_ts}"
    print(f"    query: {query}")

    payload = provider._get(ADVANCED_SEARCH.path, {"query": query, "queryType": "Latest"})
    location, tweets = locate_items(ADVANCED_SEARCH, payload)
    report.tweet_reads += len(tweets)
    print(f"    array at {location!r}, {len(tweets)} tweets")
    print(f"    top-level keys: {sorted(payload)}")
    show(check_payload(ADVANCED_SEARCH, payload))

    # The expensive thing to be wrong about: if these operators are ignored,
    # every window returns the same recent page and the backwards walk pays
    # full price to make no progress.
    section("   3a. are since_time:/until_time: honoured?")
    stamps = []
    for t in tweets:
        raw = t.get("createdAt") or t.get("created_at") or t.get("timestamp")
        try:
            stamps.append(parse_created_at(raw))
        except Exception:
            report.finding(f"unparseable createdAt in search results: {raw!r}")
    if not stamps:
        report.finding("no parseable timestamps; window honouring UNTESTED this run")
    else:
        outside = [s for s in stamps if not (since <= s <= until)]
        span = f"{min(stamps):%Y-%m-%d} .. {max(stamps):%Y-%m-%d}"
        if outside:
            report.finding(
                f"{len(outside)}/{len(stamps)} results fall OUTSIDE the requested "
                f"window {since:%Y-%m-%d}..{until:%Y-%m-%d} (got {span}). The "
                "sliding-window walk in ingest.py assumes these are honoured; if "
                "they are not, it re-reads the same page every window."
            )
        else:
            report.ok(f"all {len(stamps)} results inside the window ({span})")

    _report_cursor(payload, report)
    _report_timestamp_format(tweets, report)
    return tweets


def check_tweets_by_ids(provider: TwitterApiIoProvider, ids: list[str], report: Report) -> None:
    section(f"4. tweets_by_ids  GET {TWEETS_BY_IDS.path}")
    if not ids:
        report.finding("no ids harvested from earlier calls; batch lookup UNTESTED")
        return
    # A known-bad id alongside real ones: hydrate.py renders a missing parent as
    # "[unavailable]" on the assumption that it is simply absent from the array
    # rather than present with an error marker. Worth knowing which.
    probe = [*ids[:4], "1"]
    print(f"    tweet_ids={','.join(probe)}  ({len(probe)} ids, one deliberately bogus)")
    payload = provider._get(TWEETS_BY_IDS.path, {"tweet_ids": ",".join(probe)})
    location, tweets = locate_items(TWEETS_BY_IDS, payload)
    report.tweet_reads += len(tweets)
    print(f"    array at {location!r}, {len(tweets)} returned for {len(probe)} requested")
    show(check_payload(TWEETS_BY_IDS, payload))

    returned = {_tweet_id(t) for t in tweets}
    if "1" in returned:
        report.finding(
            "the bogus id came back as an object — hydrate.py assumes missing "
            "parents are ABSENT from the array, so it would treat this as a real "
            "parent. Check what the object contains."
        )
    else:
        report.ok("unknown ids are omitted from the array, as hydrate.py assumes")
    missing = [i for i in probe[:-1] if i not in returned]
    if missing:
        report.finding(f"{len(missing)} real ids were not returned: {missing}")


def _report_cursor(payload: dict[str, Any], report: Report) -> None:
    names = [k for k in payload if "cursor" in k.lower()]
    more = [k for k in payload if "next" in k.lower() or "more" in k.lower()]
    print(f"    cursor-ish keys: {names or '(none)'}   has-more-ish keys: {more or '(none)'}")
    if not names:
        report.finding("no cursor field found — within-window pagination would stop at page 1")


def _report_timestamp_format(tweets: list[dict[str, Any]], report: Report) -> None:
    """Tweet objects may differ from user objects. Verify rather than assume."""
    samples = {
        str(t.get("createdAt") or t.get("created_at") or t.get("timestamp")) for t in tweets[:5]
    }
    for sample in sorted(s for s in samples if s and s != "None"):
        iso = sample.endswith("Z") and "." in sample
        legacy = "+0000" in sample
        shape = "ISO+microseconds" if iso else "legacy" if legacy else "UNKNOWN"
        print(f"    createdAt sample: {sample!r} -> {shape}")
        if shape == "UNKNOWN":
            report.finding(
                f"tweet createdAt is neither confirmed format: {sample!r}. "
                "parse_created_at may be silently falling back."
            )


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--handle", default=DEFAULT_HANDLE)
    parser.add_argument("--capture-raw", metavar="DIR", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Plan and price only.")
    parser.add_argument("--force", action="store_true", help="Run even under CI. Please do not.")
    args = parser.parse_args()

    if os.environ.get("CI") and not args.force:
        print(
            "Refusing to run under CI: this script spends real money. The offline "
            "suite is the CI gate. Use --force if you truly mean it.",
            file=sys.stderr,
        )
        return 2

    print(f"{BOLD}wire contract check{OFF}  @{args.handle}  (~$0.01)")
    if args.dry_run:
        print(
            "\n--dry-run: would call user_info, last_tweets, advanced_search, "
            "tweets_by_ids. No calls made, nothing spent."
        )
        return 0

    capture = RawCapture(args.capture_raw, log=print) if args.capture_raw else None
    if capture:
        print(f"capturing raw responses to {capture.directory}")

    try:
        provider = TwitterApiIoProvider(capture=capture)
    except ProviderError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    report = Report()
    try:
        check_user_info(provider, args.handle, report)
        recent = check_last_tweets(provider, args.handle, report)
        searched = check_advanced_search(provider, args.handle, report)
        ids = [i for i in (_tweet_id(t) for t in recent + searched) if i]
        check_tweets_by_ids(provider, ids, report)
    except ProviderError as exc:
        # The failure IS the finding. Report it precisely and stop.
        print(f"\n{RED}{BOLD}PROVIDER ERROR{OFF} {exc}", file=sys.stderr)
        print(f"spent ~${report.spend:.4f} before failing", file=sys.stderr)
        return 1
    finally:
        provider.close()

    section("summary")
    critical = [v for v in report.violations if v.severity == CRITICAL]
    important = [v for v in report.violations if v.severity == IMPORTANT]
    print(
        f"    spend:      ~${report.spend:.4f} "
        f"({report.profile_reads} profile, {report.tweet_reads} tweet reads)"
    )
    print(f"    critical:   {len(critical)}")
    print(f"    important:  {len(important)}")
    print(f"    findings:   {len(report.findings)}")
    if capture:
        print(f"    captured:   {capture.count} responses -> {capture.directory}")

    if critical:
        print(
            f"\n{RED}{BOLD}CONTRACT BROKEN{OFF} — the provider changed shape. Fix "
            "normalize_tweet / _tweets_from / _cursor_from, update "
            "corpus/x/contract.py and docs/wire-contract.md, then re-run."
        )
        return 1
    if important or report.findings:
        print(
            f"\n{YELLOW}drift worth reading{OFF} — nothing is broken, but the "
            "notes above differ from what docs/wire-contract.md records."
        )
        return 0
    print(f"\n{GREEN}{BOLD}clean{OFF} — provider matches the recorded contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
