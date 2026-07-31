"""corpus — CLI.

    corpus run --x paulg
    corpus run --x paulg --max-posts 5000 --since 2020-01-01 --budget 15
    corpus run --x someone --dry-run
    corpus run --x someone --also-substack example.com
    corpus resynth out/paulg/2026-07-31
    corpus cache stats | corpus cache clear
    corpus budget log
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

from .budget import (
    BUDGET_MODES,
    STRICT,
    Budget,
    BudgetExceeded,
    estimate_anthropic_cost,
    estimate_x_cost,
)
from .cache import Cache, DEFAULT_TTL_SECONDS
from .models import Document, Synthesis
from .render import render_report
from .synthesize import synthesize
from .x.capture import RawCapture
from .x.client import XClient
from .x.hydrate import hydrate
from .x.ingest import ingest_timeline
from .x.providers import ProviderError, get_provider
from .x.signals import compute_signals

app = typer.Typer(
    add_completion=False,
    help="Ingest a person's public X history, hydrate it, and synthesize what they think.",
)
cache_app = typer.Typer(help="Inspect and clear the SQLite content cache.")
budget_app = typer.Typer(help="Spend history.")
app.add_typer(cache_app, name="cache")
app.add_typer(budget_app, name="budget")

load_dotenv()


def echo(msg: str = "") -> None:
    typer.echo(msg)


@dataclass
class _GlobalOptions:
    """Options that apply to every command, set by the app callback below."""

    capture_raw: Path | None = None


_GLOBALS = _GlobalOptions()


@app.callback()
def _main(
    capture_raw: Path | None = typer.Option(
        None,
        "--capture-raw",
        metavar="DIR",
        help=(
            "Dump every raw provider response to DIR verbatim, one JSON file per "
            "call, before any normalization. For verifying the wire contract."
        ),
    ),
) -> None:
    """Global options."""
    _GLOBALS.capture_raw = capture_raw


def _make_capture(local: Path | None = None) -> RawCapture | None:
    """Resolve --capture-raw from either position.

    It is documented as a global flag, but `corpus run ... --capture-raw DIR`
    reads more naturally and is what anyone actually types, so `run` accepts it
    too. A flag that only works in one of the two obvious positions is a flag
    people give up on.
    """
    directory = local or _GLOBALS.capture_raw
    if directory is None:
        return None
    capture = RawCapture(directory, log=echo)
    echo(f"  [capture] raw responses -> {capture.directory}")
    return capture


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _out_dir(base: Path, handle: str) -> Path:
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    path = base / handle.lstrip("@") / day
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------


@app.command()
def run(
    x: str = typer.Option(..., "--x", help="X handle, with or without @."),
    max_posts: int = typer.Option(3000, "--max-posts"),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD floor."),
    budget_limit: float = typer.Option(10.00, "--budget", help="Hard stop, in dollars."),
    budget_mode: str = typer.Option(
        STRICT,
        "--budget-mode",
        help=(
            "strict: refuse any call that cannot be fully reserved (default). "
            "advisory: reserve and report, but never block."
        ),
    ),
    window_days: int = typer.Option(30, "--window-days"),
    empty_window_tolerance: int = typer.Option(3, "--empty-window-tolerance"),
    max_pages: int = typer.Option(20, "--max-pages", help="Cursor pages per window."),
    include_reposts: bool = typer.Option(False, "--include-reposts"),
    replies: bool = typer.Option(True, "--replies/--no-replies"),
    substack: str | None = typer.Option(None, "--substack", "--also-substack", help="Substack domain."),
    rss: list[str] = typer.Option([], "--rss", help="Feed URL. Repeatable."),
    url: list[str] = typer.Option([], "--url", help="Single page URL. Repeatable."),
    out: Path = typer.Option(Path("out"), "--out"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the cache."),
    offline: bool = typer.Option(False, "--offline", help="Cache only; no network."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate and plan only."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend confirmation."),
    skip_synthesis: bool = typer.Option(False, "--skip-synthesis"),
    map_effort: str = typer.Option("medium", "--map-effort", help="low|medium|high|xhigh|max"),
    reduce_effort: str = typer.Option("high", "--reduce-effort"),
    cache_ttl_days: int = typer.Option(7, "--cache-ttl-days"),
    capture_raw: Path | None = typer.Option(
        None,
        "--capture-raw",
        metavar="DIR",
        help="Dump every raw provider response to DIR verbatim, before normalization.",
    ),
) -> None:
    """Ingest, hydrate, and synthesize one person's public writing."""
    handle = x.lstrip("@")
    since_dt = (
        datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if since else None
    )

    cache = Cache(
        ttl_seconds=cache_ttl_days * 24 * 3600 if cache_ttl_days else DEFAULT_TTL_SECONDS,
        refresh=refresh,
        offline=offline,
    )
    if budget_mode not in BUDGET_MODES:
        echo(f"ERROR: --budget-mode must be one of {', '.join(BUDGET_MODES)}")
        raise typer.Exit(code=2)
    budget = Budget(limit=budget_limit, cache=cache, mode=budget_mode)

    echo(f"corpus run @{handle} (run {budget.run_id})")
    echo(f"  budget ${budget_limit:.2f} ({budget_mode}) · max-posts {max_posts} "
         f"· window {window_days}d"
         + (f" · since {since}" if since else "")
         + (" · OFFLINE" if offline else ""))
    echo("")

    # ---- estimate + confirm ---------------------------------------------
    raw_tweets: list[dict[str, Any]] = []
    client: XClient | None = None
    profile: dict[str, Any] = {}

    if offline:
        cached = cache.get("x", f"corpus:{handle.lower()}")
        if cached is None:
            echo(f"ERROR: --offline but no cached corpus for @{handle}.")
            echo("Run once without --offline first.")
            raise typer.Exit(code=2)
        raw_tweets = cached
        echo(f"  loaded {len(raw_tweets)} cached raw posts")
    else:
        try:
            provider = get_provider(capture=_make_capture(capture_raw))
        except (ProviderError, NotImplementedError) as exc:
            echo(f"ERROR: {exc}")
            raise typer.Exit(code=2)
        client = XClient(provider, cache, budget)
        try:
            profile = client.user_info(handle)
        except ProviderError as exc:
            echo(f"ERROR fetching profile for @{handle}: {exc}")
            raise typer.Exit(code=2)

        public_posts = int(
            profile.get("statusesCount")
            or profile.get("statuses_count")
            or profile.get("tweetCount")
            or 0
        )
        target = min(max_posts, public_posts) if public_posts else max_posts
        x_cost = estimate_x_cost(target)
        llm_cost = 0.0 if skip_synthesis else estimate_anthropic_cost(target)

        echo(f"  @{handle}: {public_posts or 'unknown'} public posts on file")
        echo(f"  estimate: ~{target} posts")
        echo(f"    X data (twitterapi.io): ~${x_cost:.3f}")
        echo(f"    Anthropic synthesis:    ~${llm_cost:.3f}")
        echo(f"    total:                  ~${x_cost + llm_cost:.3f} of ${budget_limit:.2f} budget")
        if x_cost + llm_cost > budget_limit:
            echo("  WARNING: the estimate exceeds the budget. The run will stop early.")
        echo("")

        if dry_run:
            echo("--dry-run: stopping before any paid fetch.")
            echo(f"  spent so far: ${budget.total:.4f} (profile lookup)")
            raise typer.Exit(code=0)

        if not yes and not typer.confirm("Proceed?", default=True):
            echo("Aborted.")
            raise typer.Exit(code=0)
        echo("")

        # ---- ingest ------------------------------------------------------
        echo("Ingesting (sliding time window):")
        raw_tweets, ingest_stats = ingest_timeline(
            client,
            handle,
            since=since_dt,
            max_posts=max_posts,
            window_days=window_days,
            empty_window_tolerance=empty_window_tolerance,
            max_pages=max_pages,
            include_replies=replies,
            log=echo,
        )
        cache.put("x", f"corpus:{handle.lower()}", raw_tweets)

    if dry_run:
        echo("--dry-run with --offline: nothing to estimate.")
        raise typer.Exit(code=0)

    ingest_meta = (
        ingest_stats.as_dict() if not offline else {"stop_reason": "loaded from cache (--offline)"}
    )
    echo(f"  {len(raw_tweets)} unique posts · ${budget.total:.4f} spent")
    echo("")

    if not raw_tweets:
        echo("ERROR: no posts ingested. Nothing to synthesize.")
        raise typer.Exit(code=1)

    # ---- hydrate ---------------------------------------------------------
    echo("Hydrating:")
    if client is None:
        provider = _OfflineProvider()
        client = XClient(provider, cache, budget)  # type: ignore[arg-type]
    try:
        docs, hyd_stats = hydrate(
            client,
            raw_tweets,
            handle,
            include_reposts=include_reposts,
            include_replies=replies,
            log=echo,
        )
    except BudgetExceeded as exc:
        # Un-hydrated documents are still worth keeping — we paid for them.
        echo(f"  [budget] {exc} — continuing with un-hydrated documents")
        from .x.client import normalize_tweet
        from .x.hydrate import HydrationStats

        docs = [normalize_tweet(t) for t in raw_tweets]
        hyd_stats = HydrationStats(
            input_documents=len(raw_tweets),
            output_documents=len(docs),
            notes=["budget exhausted before hydration completed; context is missing"],
        )
    echo("")

    # ---- secondary sources ----------------------------------------------
    secondary = _fetch_secondary(handle, substack, rss, url, cache, echo)
    if secondary:
        docs.extend(secondary)
        docs.sort(key=lambda d: d.published_at, reverse=True)
        echo("")

    # ---- signals ---------------------------------------------------------
    echo("Computing signals (Python, no API calls)...")
    signals = compute_signals(
        docs, extra={"ingest": ingest_meta, "hydration": hyd_stats.as_dict()}
    )
    echo(
        f"  {signals['total_documents']} documents, "
        f"{len(signals['conversation_graph'])} network handles, "
        f"{len(signals['outbound_domains'])} domains, "
        f"{len(signals['vocabulary_drift'])} vocabulary buckets"
    )
    echo("")

    # ---- write corpus + signals before spending on synthesis -------------
    out_dir = _out_dir(out, handle)
    _write_json(out_dir / "corpus.json", [d.model_dump() for d in docs])
    _write_json(out_dir / "signals.json", signals)
    echo(f"Wrote corpus.json and signals.json to {out_dir}")
    echo("")

    # ---- synthesize ------------------------------------------------------
    run_meta: dict[str, Any] = {
        "ingest": ingest_meta,
        "hydration": hyd_stats.as_dict(),
        "budget_stopped": budget.stopped,
    }
    synthesis: Synthesis | None = None
    exit_code = 0

    if skip_synthesis:
        run_meta["synthesis_error"] = "skipped (--skip-synthesis)"
        echo("Skipping synthesis (--skip-synthesis).")
    elif budget.remaining <= 0:
        run_meta["synthesis_error"] = "budget exhausted before synthesis"
        echo("Budget exhausted before synthesis; corpus preserved.")
    else:
        echo("Synthesizing (map -> reduce):")
        result = asyncio.run(
            synthesize(
                docs,
                signals,
                budget,
                map_effort=map_effort,
                reduce_effort=reduce_effort,
                log=echo,
            )
        )
        synthesis = result.synthesis
        run_meta["synthesis_error"] = result.error
        run_meta["dropped_findings"] = result.dropped_findings
        run_meta["corrected_counts"] = result.corrected_counts
        run_meta["budget_stopped"] = budget.stopped
        if synthesis is not None:
            _write_json(out_dir / "synthesis.json", synthesis.model_dump())
            echo(f"  wrote synthesis.json ({len(synthesis.themes)} themes, "
                 f"{len(synthesis.positions)} positions)")
        else:
            if result.raw_reduce_output:
                (out_dir / "reduce_raw_output.txt").write_text(
                    result.raw_reduce_output, encoding="utf-8"
                )
                echo("  dumped unparseable model output to reduce_raw_output.txt")
            echo(f"  SYNTHESIS FAILED: {result.error}")
            if not budget.stopped:
                exit_code = 1
    echo("")

    # ---- report + spend --------------------------------------------------
    report = render_report(
        handle=handle,
        synthesis=synthesis,
        docs=docs,
        signals=signals,
        budget_lines=budget.summary_lines(),
        run_meta=run_meta,
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    echo("Spend:")
    for line in budget.summary_lines():
        echo(f"  {line}")
    echo("")
    echo(f"Report: {out_dir / 'report.md'}")

    if budget.stopped:
        echo("")
        echo("WARNING: the budget was exhausted. Results are partial but every paid")
        echo("byte was written to disk. Re-run with a higher --budget to continue.")
        raise typer.Exit(code=0)  # partial results preserved, not a failure

    cache.close()
    raise typer.Exit(code=exit_code)


class _OfflineProvider:
    """Stands in for a provider when running from cache with no network."""

    name = "offline"

    def user_info(self, handle: str) -> dict[str, Any]:
        raise ProviderError("offline")

    def last_tweets(self, handle: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        raise ProviderError("offline")

    def advanced_search(self, query: str, cursor: str | None = None):  # type: ignore[no-untyped-def]
        raise ProviderError("offline")

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        return None


def _fetch_secondary(
    handle: str,
    substack: str | None,
    rss: list[str],
    urls: list[str],
    cache: Cache,
    log: Any,
) -> list[Document]:
    docs: list[Document] = []
    if not (substack or rss or urls):
        return docs
    log("Secondary sources (optional bolt-ons; X remains the corpus):")
    from .sources.base import SourceError

    if substack:
        from .sources.substack import SubstackSource

        try:
            docs.extend(
                SubstackSource().fetch(substack, author_handle=handle, cache=cache, log=log)
            )
        except SourceError as exc:
            log(f"  substack failed (non-fatal): {exc}")
    for feed in rss:
        from .sources.rss import RSSSource

        try:
            docs.extend(RSSSource().fetch(feed, author_handle=handle, cache=cache, log=log))
        except SourceError as exc:
            log(f"  rss {feed} failed (non-fatal): {exc}")
    for page in urls:
        from .sources.web import WebSource

        try:
            docs.extend(WebSource().fetch(page, author_handle=handle, cache=cache, log=log))
        except SourceError as exc:
            log(f"  web {page} failed (non-fatal): {exc}")
    return docs


# --------------------------------------------------------------------------


@app.command()
def resynth(
    directory: Path = typer.Argument(..., help="An existing out/<handle>/<date> directory."),
    budget_limit: float = typer.Option(10.00, "--budget"),
    map_effort: str = typer.Option("medium", "--map-effort"),
    reduce_effort: str = typer.Option("high", "--reduce-effort"),
) -> None:
    """Re-run synthesis on a cached corpus. No fetching, no X spend."""
    corpus_path = directory / "corpus.json"
    signals_path = directory / "signals.json"
    if not corpus_path.exists() or not signals_path.exists():
        echo(f"ERROR: {directory} must contain corpus.json and signals.json")
        raise typer.Exit(code=2)

    docs = [Document.model_validate(d) for d in json.loads(corpus_path.read_text())]
    signals = json.loads(signals_path.read_text())
    handle = signals.get("author_handle") or (docs[0].author_handle if docs else "unknown")

    cache = Cache()
    budget = Budget(limit=budget_limit, cache=cache)
    echo(f"resynth @{handle}: {len(docs)} documents, no fetching (run {budget.run_id})")
    echo("")

    result = asyncio.run(
        synthesize(
            docs, signals, budget, map_effort=map_effort, reduce_effort=reduce_effort, log=echo
        )
    )
    run_meta = {
        "ingest": signals.get("ingest", {}),
        "hydration": signals.get("hydration", {}),
        "synthesis_error": result.error,
        "dropped_findings": result.dropped_findings,
        "corrected_counts": result.corrected_counts,
        "budget_stopped": budget.stopped,
    }

    if result.synthesis is not None:
        _write_json(directory / "synthesis.json", result.synthesis.model_dump())
    elif result.raw_reduce_output:
        (directory / "reduce_raw_output.txt").write_text(
            result.raw_reduce_output, encoding="utf-8"
        )
        echo("  dumped unparseable model output to reduce_raw_output.txt")

    report = render_report(
        handle=handle,
        synthesis=result.synthesis,
        docs=docs,
        signals=signals,
        budget_lines=budget.summary_lines(),
        run_meta=run_meta,
    )
    (directory / "report.md").write_text(report, encoding="utf-8")

    echo("")
    for line in budget.summary_lines():
        echo(f"  {line}")
    echo(f"Report: {directory / 'report.md'}")
    cache.close()
    raise typer.Exit(code=0 if result.synthesis is not None else 1)


@cache_app.command("stats")
def cache_stats() -> None:
    """Cache size, entry counts, and age."""
    cache = Cache()
    stats = cache.stats()
    echo(f"path:    {stats['path']}")
    echo(f"entries: {stats['entries']}")
    echo(f"size:    {stats['size_bytes'] / 1024:.1f} KiB")
    if stats["oldest_fetch"]:
        oldest = datetime.fromtimestamp(stats["oldest_fetch"], tz=timezone.utc)
        echo(f"oldest:  {oldest.strftime('%Y-%m-%d %H:%M UTC')}")
    for source, counts in sorted(stats["by_source"].items()):
        echo(f"  {source}: {counts['entries']} entries ({counts['permanent']} permanent)")
    cache.close()


@cache_app.command("clear")
def cache_clear(
    keep_permanent: bool = typer.Option(
        False,
        "--keep-permanent",
        help="Keep hydrated parents (old tweets never change; re-fetching costs money).",
    ),
) -> None:
    """Empty the content cache."""
    cache = Cache()
    removed = cache.clear(keep_permanent=keep_permanent)
    echo(f"removed {removed} entries from {cache.path}")
    cache.close()


@budget_app.command("log")
def budget_log(limit: int = typer.Option(50, "--limit")) -> None:
    """Every billable call, most recent first."""
    cache = Cache()
    rows = cache.spend_log(limit=limit)
    if not rows:
        echo("no spend recorded yet")
        cache.close()
        return
    echo(f"{'when':<20} {'run':<14} {'category':<10} {'endpoint':<28} {'units':>9} {'cost':>10}")
    echo("-" * 96)
    for row in rows:
        when = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        echo(
            f"{when:<20} {row['run_id']:<14} {row['category']:<10} "
            f"{row['endpoint'][:28]:<28} {row['units']:>9.0f} ${row['cost']:>9.4f}"
        )
    echo("")
    for row in cache.spend_totals():
        echo(f"total {row['category']:<10} ${row['total']:.4f} across {row['calls']} calls")
    cache.close()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        echo("\ninterrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
