"""corpus — CLI.

corpus profile --name "Jane Smith" --employer "Acme Corp" --github jsmith
corpus profile --target jane
corpus run --target jane
corpus run --x paulg
corpus run --github jsmith --site https://janesmith.com    # no X at all
corpus run --x paulg --max-posts 5000 --since 2020-01-01 --budget 15
corpus run --x someone --dry-run
corpus resynth out/paulg/2026-08-02
    corpus resynth out/paulg/2026-08-02 --render-only   # re-render, zero API calls
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

from .axes import AxisError, select_axes
from .budget import (
    BUDGET_MODES,
    STRICT,
    Budget,
    BudgetExceeded,
    estimate_anthropic_split,
    estimate_search_phase,
    estimate_x_cost,
)
from .cache import DEFAULT_TTL_SECONDS, Cache
from .discovery import (
    DEFAULT_MAX_FETCHES,
    Candidate,
    DiscoveryResult,
    discover_from_anchors,
    kind_for,
    plan_from_anchors,
)
from .identity import (
    IdentityCard,
    IdentityError,
    build_card,
    default_profiles_path,
    load_target,
    load_targets,
    merge_flags,
    save_target,
)
from .logging_setup import LOG_FORMATS, TEXT, RunLogger
from .manifest import RunManifest
from .models import Document, Synthesis
from .render import render_report
from .search.capture import SearchCapture
from .search.client import SearchClient
from .search.providers import SearchError, get_search_provider
from .search.queries import DEFAULT_MAX_SEARCHES, generate_queries
from .search.unconfirmed import read_unconfirmed, write_unconfirmed
from .search.verify import (
    DEFAULT_MAX_VERIFY_FETCHES,
    SearchPhaseResult,
    search_for_sources,
)
from .synthesize import DEFAULT_HIGHLIGHTS, MAP_MODEL, REDUCE_MODEL, synthesize
from .tiers import THIN_BELOW
from .x.capture import RawCapture
from .x.client import XClient
from .x.hydrate import hydrate
from .x.ingest import DEFAULT_EMPTY_WINDOW_TOLERANCE, ingest_timeline
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


# The active run's logger, if a run is in progress.
#
# A module global rather than a threaded-through parameter: this is a CLI with
# one run per process, and the alternative is rewriting every echo() call site
# in this file to carry a logger it would only ever pass along. Commands that
# are pure output (cache stats, budget log) leave it None and print directly —
# their output is a result, not a log.
_ACTIVE_LOGGER: RunLogger | None = None


def echo(msg: str = "") -> None:
    """Emit a line — through the run logger when one is active."""
    if _ACTIVE_LOGGER is not None:
        _ACTIVE_LOGGER.logger.info(msg)
    else:
        typer.echo(msg)


def warn(msg: str) -> None:
    """A line --quiet must never suppress."""
    if _ACTIVE_LOGGER is not None:
        _ACTIVE_LOGGER.logger.warning(msg)
    else:
        typer.echo(f"WARNING: {msg}")


def error(msg: str) -> None:
    if _ACTIVE_LOGGER is not None:
        _ACTIVE_LOGGER.logger.error(msg)
    else:
        typer.echo(f"ERROR: {msg}")


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
    x: str | None = typer.Option(
        None, "--x", help="X handle, with or without @. Optional — a run with no X anchor works."
    ),
    target: str | None = typer.Option(
        None, "--target", help="A saved target from profiles.yaml. See `corpus profile`."
    ),
    name: str = typer.Option("", "--name", help="Their full name, for scoring what is found."),
    employer: str = typer.Option("", "--employer"),
    role: str = typer.Option("", "--role"),
    location: str = typer.Option("", "--location", help="Disambiguates a common name."),
    github: str = typer.Option("", "--github", help="GitHub username. An anchor."),
    site: str = typer.Option("", "--site", help="Their own site. An anchor, and crawled."),
    bluesky: str = typer.Option(
        "", "--bluesky", help="Bluesky handle, e.g. janesmith.bsky.social. An anchor."
    ),
    hn: str = typer.Option("", "--hn", help="Hacker News username. An anchor."),
    reddit: str = typer.Option("", "--reddit", help="Reddit username. An anchor."),
    mastodon: str = typer.Option(
        "", "--mastodon", help="Mastodon address, e.g. @user@instance. An anchor."
    ),
    profiles: Path | None = typer.Option(None, "--profiles", metavar="PATH"),
    discover: bool = typer.Option(
        True,
        "--discover/--no-discover",
        help="Follow links out from the anchors. Free, and never fatal. Anchors are read either way.",
    ),
    max_fetches: int = typer.Option(
        DEFAULT_MAX_FETCHES, "--max-fetches", help="Ceiling on discovery's plain-HTTP requests."
    ),
    search: bool = typer.Option(
        True,
        "--search/--no-search",
        help=(
            "Phase 2: search for sources the anchors do not reach. Costs money "
            "(~$0.01 per query) and never ingests anything it cannot verify."
        ),
    ),
    max_searches: int = typer.Option(
        DEFAULT_MAX_SEARCHES,
        "--max-searches",
        help="Ceiling on billable search queries. Cached queries are free and do not count.",
    ),
    max_verify_fetches: int = typer.Option(
        DEFAULT_MAX_VERIFY_FETCHES,
        "--max-verify-fetches",
        help="Ceiling on pages fetched to verify search candidates. Free, plain HTTP.",
    ),
    accept_unconfirmed: Path | None = typer.Option(
        None,
        "--accept-unconfirmed",
        metavar="PATH",
        help=(
            "Read back an edited unconfirmed.md: checked entries are ingested as "
            "corroborated, unchecked ones are added to the card's exclude list."
        ),
    ),
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
    empty_window_tolerance: int = typer.Option(
        DEFAULT_EMPTY_WINDOW_TOLERANCE,
        "--empty-window-tolerance",
        help=(
            "Consecutive empty windows before giving up. On reaching it, one wide "
            "probe checks whether the silence is real before stopping."
        ),
    ),
    hiatus_probe: bool = typer.Option(
        True,
        "--hiatus-probe/--no-hiatus-probe",
        help="Sweep 12 months in one call before concluding history has ended.",
    ),
    max_pages: int = typer.Option(20, "--max-pages", help="Cursor pages per window."),
    include_reposts: bool = typer.Option(False, "--include-reposts"),
    replies: bool = typer.Option(True, "--replies/--no-replies"),
    substack: str | None = typer.Option(
        None, "--substack", "--also-substack", help="Substack domain."
    ),
    rss: list[str] = typer.Option([], "--rss", help="Feed URL. Repeatable."),
    url: list[str] = typer.Option([], "--url", help="Single page URL. Repeatable."),
    out: Path = typer.Option(Path("out"), "--out"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the cache."),
    offline: bool = typer.Option(False, "--offline", help="Cache only; no network."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate and plan only."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend confirmation."),
    skip_synthesis: bool = typer.Option(False, "--skip-synthesis"),
    axes: str | None = typer.Option(
        None, "--axes", help="Comma-separated worldview axes. Default: all in profiles.yaml."
    ),
    map_model: str = typer.Option(MAP_MODEL, "--map-model"),
    reduce_model: str = typer.Option(REDUCE_MODEL, "--reduce-model"),
    map_effort: str = typer.Option("medium", "--map-effort", help="low|medium|high|xhigh|max"),
    reduce_effort: str = typer.Option("high", "--reduce-effort"),
    highlights: int = typer.Option(
        DEFAULT_HIGHLIGHTS,
        "--highlights",
        help="Complete documents pasted into the reduce prompt. The biggest cost "
        "lever in the tool: ~60% of a real run's spend at the default.",
    ),
    no_filter: bool = typer.Option(
        False, "--no-filter", help="Keep low-signal documents (acks, link-only, fragments)."
    ),
    cache_ttl_days: int = typer.Option(7, "--cache-ttl-days"),
    capture_raw: Path | None = typer.Option(
        None,
        "--capture-raw",
        metavar="DIR",
        help="Dump every raw provider response to DIR verbatim, before normalization.",
    ),
    capture_search: Path | None = typer.Option(
        None,
        "--capture-search",
        metavar="DIR",
        help="Dump every raw search response to DIR verbatim, before it is interpreted.",
    ),
    log_format: str = typer.Option(
        TEXT, "--log-format", help="text (default, human-readable) | json (one object per line)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Include phase and elapsed time on every line."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Warnings and errors only. Never hides a budget stop."
    ),
    resume: Path | None = typer.Option(
        None,
        "--resume",
        metavar="PATH",
        help="Resume from a previous run's out/<handle>/<date> directory.",
    ),
) -> None:
    """Find one person's public writing, and synthesize how they think."""
    if log_format not in LOG_FORMATS:
        typer.echo(f"ERROR: --log-format must be one of {', '.join(LOG_FORMATS)}")
        raise typer.Exit(code=2)
    # The card is resolved at the boundary, where an error can name the input
    # and the user can still fix it. Anchors are validated on the way in: an
    # unvalidated X handle does not fail downstream, it silently changes what
    # the search query means.
    try:
        card = _resolve_card(
            target=target,
            profiles=profiles,
            name=name,
            employer=employer,
            role=role,
            location=location,
            x=x or "",
            github=github,
            site=site,
            substack=substack or "",
            bluesky=bluesky,
            hn=hn,
            reddit=reddit,
            mastodon=mastodon,
        )
    except IdentityError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc
    handle = card.x_handle
    try:
        selected_axes = select_axes(axes)
    except AxisError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc
    try:
        since_dt = (
            datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if since else None
        )
    except ValueError as exc:
        error(f"--since {since!r} is not a date. Expected YYYY-MM-DD.")
        raise typer.Exit(code=2) from exc

    cache = Cache(
        ttl_seconds=cache_ttl_days * 24 * 3600 if cache_ttl_days else DEFAULT_TTL_SECONDS,
        refresh=refresh,
        offline=offline,
    )
    if budget_mode not in BUDGET_MODES:
        echo(f"ERROR: --budget-mode must be one of {', '.join(BUDGET_MODES)}")
        raise typer.Exit(code=2)
    budget = Budget(limit=budget_limit, cache=cache, mode=budget_mode)

    # The logger keys on the budget's run_id, so a line in the terminal and a
    # row in `corpus budget log` can be tied together after the fact.
    global _ACTIVE_LOGGER
    _ACTIVE_LOGGER = RunLogger(budget.run_id, log_format=log_format, verbose=verbose, quiet=quiet)

    # ---- resume ---------------------------------------------------------
    manifest = RunManifest.load(resume) if resume else None
    if resume and manifest is None:
        error(
            f"--resume {resume}: no usable run.json there. A corrupt or "
            f"future-version manifest is ignored rather than trusted; re-run "
            f"without --resume to start fresh."
        )
        raise typer.Exit(code=2)
    if manifest is not None:
        if manifest.handle and manifest.handle != card.key:
            error(f"--resume {resume} is a run for {manifest.handle}, not {card.key}")
            raise typer.Exit(code=2)
        # A resumed run's budget covers everything the target has cost, not
        # just this attempt — otherwise --budget 10 resumed three times is a $30
        # run, and the flag documented as a hard stop would be per-attempt.
        budget.prior_spend = manifest.prior_spend

    echo(f"corpus run {card.display} (run {budget.run_id})")
    echo(f"  anchors: {_anchor_line(card)}")
    echo(
        f"  budget ${budget_limit:.2f} ({budget_mode}) · max-posts {max_posts} "
        f"· window {window_days}d"
        + (f" · since {since}" if since else "")
        + (" · OFFLINE" if offline else "")
    )
    echo("")

    # The output directory is created up front, not after ingestion: the
    # manifest is checkpointed during the walk, and a checkpoint needs
    # somewhere to land before the thing it protects against happens.
    out_dir = Path(resume) if resume else _out_dir(out, card.key)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest or RunManifest(handle=card.key, run_id=budget.run_id)
    manifest.handle = manifest.handle or card.key
    manifest.run_id = budget.run_id

    # ---- accept an edited unconfirmed.md ---------------------------------
    # Before discovery, so the rejections are already excluded by the time
    # anything is scored against the card.
    accepted_urls: list[str] = []
    if accept_unconfirmed is not None:
        try:
            card, accepted_urls = _accept_unconfirmed(
                accept_unconfirmed, card, profiles, target, assume_yes=yes
            )
        except (IdentityError, OSError) as exc:
            error(f"--accept-unconfirmed {accept_unconfirmed}: {exc}")
            raise typer.Exit(code=2) from exc

    # ---- estimate + confirm ---------------------------------------------
    estimated_total: float | None = None
    estimated_posts: int = 0
    raw_tweets: list[dict[str, Any]] = []
    client: XClient | None = None
    profile: dict[str, Any] = {}
    public_posts = 0
    post_target = 0
    # Why the X source failed, when it did. Empty means it did not. The status
    # is "partial" when the checkpoints had already banked some posts.
    x_failure = ""
    x_status = "ok"

    if handle and offline:
        cached = cache.get("x", f"corpus:{handle.lower()}")
        if cached is None:
            echo(f"ERROR: --offline but no cached corpus for @{handle}.")
            echo("Run once without --offline first.")
            raise typer.Exit(code=2)
        raw_tweets = cached
        echo(f"  loaded {len(raw_tweets)} cached raw posts")
    elif handle:
        try:
            provider = get_provider(capture=_make_capture(capture_raw), log=echo)
        except (ProviderError, NotImplementedError) as exc:
            error(str(exc))
            raise typer.Exit(code=2) from exc
        client = XClient(provider, cache, budget)
        try:
            profile = client.user_info(handle)
        except ProviderError as exc:
            error(f"fetching profile for @{handle}: {exc}")
            raise typer.Exit(code=2) from exc

        public_posts = int(
            profile.get("statusesCount")
            or profile.get("statuses_count")
            or profile.get("tweetCount")
            or 0
        )
        post_target = min(max_posts, public_posts) if public_posts else max_posts

    # ---- discovery (phase 1: follow the anchors) -------------------------
    # Before the estimate, because what it finds changes what the corpus will
    # be — and before the confirmation, because it costs nothing to run and a
    # spend prompt should describe the run that is actually about to happen.
    _ACTIVE_LOGGER.context.phase = "discover"
    discovery = discover_from_anchors(
        card,
        cache,
        x_profile=profile or None,
        # The pinned-post read is the only metered call in the phase, so it is
        # skipped under --dry-run, which promises to stop before paid fetches.
        x_lookup=(client.tweets_by_ids if client is not None and not dry_run else None),
        follow_links=discover,
        max_fetches=max_fetches,
        log=echo,
    )
    _report_discovery(discovery, card, following=discover)
    echo("")

    if not handle and not (rss or url) and not discovery.candidates:
        error(
            "no sources. There is no X anchor, discovery found nothing, and no "
            "--rss/--url was given. Add an anchor (--github, --site, --substack, "
            "--bluesky, --hn, --reddit, --mastodon) or name a saved target with --target."
        )
        raise typer.Exit(code=2)

    # ---- everything that is not X ----------------------------------------
    # Read *before* the estimate, not after, because it is free — plain HTTP,
    # no metered API — and because on a run with no X anchor an estimate that
    # ignores it is an estimate of nothing. The prompt below then describes the
    # run that is actually about to happen.
    _ACTIVE_LOGGER.context.phase = "sources"
    author = handle or card.slug or card.key
    source_notes: list[str] = []
    other = _fetch_discovered(discovery.candidates, author, cache, echo, source_notes)
    other.extend(_fetch_secondary(author, rss, url, cache, echo, source_notes))
    other.extend(_fetch_accepted(accepted_urls, author, cache, echo, source_notes))
    if other:
        echo(
            f"  {len(other)} document(s) from {len(discovery.candidates) + len(rss) + len(url)} "
            f"non-X source(s), at no cost"
        )
        echo("")

    planned_queries = generate_queries(card, max_searches) if search else []

    if not offline:
        x_cost = estimate_x_cost(post_target) if handle else 0.0
        search_cost = estimate_search_phase(len(planned_queries)) if planned_queries else 0.0
        projected_docs = post_target + len(other)
        map_cost, reduce_cost = (
            (0.0, 0.0)
            if skip_synthesis
            # The models actually configured for this run, so the estimate
            # moves with --reduce-model instead of assuming a default.
            else estimate_anthropic_split(projected_docs, map_model, reduce_model)
        )
        estimated_total = x_cost + search_cost + map_cost + reduce_cost
        estimated_posts = post_target
        if handle:
            echo(f"  @{handle}: {public_posts or 'unknown'} public posts on file")
        echo(
            f"  estimate: ~{post_target} posts + {len(other)} document(s) already "
            f"read from {len(discovery.candidates)} discovered source(s)"
        )
        echo(f"    discovery (plain HTTP):   $0.000  ({discovery.fetches} request(s))")
        echo(f"    fetch — X data:          ~${x_cost:.3f}")
        echo(
            f"    search (phase 2):        ~${search_cost:.3f}  "
            f"({len(planned_queries)} quer{'y' if len(planned_queries) == 1 else 'ies'})"
        )
        echo(f"    map:                     ~${map_cost:.3f}")
        echo(f"    reduce:                  ~${reduce_cost:.3f}")
        echo(f"    total:                   ~${estimated_total:.3f} of ${budget_limit:.2f} budget")
        if estimated_total > budget_limit:
            warn("the estimate exceeds the budget; the run will stop early")
        if projected_docs and projected_docs < THIN_BELOW and not skip_synthesis:
            echo("")
            warn(
                f"~{projected_docs} documents is under the {THIN_BELOW}-document floor, so "
                "this will be a THIN corpus: inferred positions, blind spots, and view "
                "changes are all suppressed and the report shows stated positions only."
            )
            echo("  Other sources merge into the same corpus and cost nothing —")
            echo("  they are plain HTTP, not a metered API:")
            echo("    --github USER --site URL     # anchors, and then crawled for more")
            echo("    --bluesky HANDLE             # posts, replies, stitched threads")
            echo("    --hn USER --reddit USER      # years of public arguments")
            echo("    --mastodon @user@instance")
            echo("    --substack DOMAIN")
            echo("    --rss URL                    # repeatable")
            echo("    --url URL                    # repeatable")
            if handle:
                echo("  Or reach further back: raise --max-posts, drop --since, or raise")
                echo("  --empty-window-tolerance.")
        echo("")

        if dry_run:
            echo("--dry-run: stopping before any paid fetch.")
            for line in plan_from_anchors(card):
                echo(f"  plan: {line}")
            if planned_queries:
                echo(f"  plan: {len(planned_queries)} search quer(y/ies), none of them run:")
                for query in planned_queries:
                    echo(f"    search: {query.text}   ({query.why})")
            else:
                echo("  plan: 0 searches" + ("" if search else " (--no-search)"))
            spent = f"${budget.total:.4f}" + (" (profile lookup)" if handle else "")
            echo(f"  spent so far: {spent}")
            raise typer.Exit(code=0)

        if not yes and not typer.confirm("Proceed?", default=True):
            echo("Aborted.")
            raise typer.Exit(code=0)
        echo("")

    # ---- discovery (phase 2: search) -------------------------------------
    # After the confirmation, because unlike phase 1 this phase costs money.
    # Before ingestion, so a budget stop during the X walk cannot throw away
    # search results that were already paid for.
    search_result = SearchPhaseResult()
    if search:
        _ACTIVE_LOGGER.context.phase = "search"
        search_result = _run_search(
            card,
            cache,
            budget,
            max_searches=max_searches,
            max_fetches=max_verify_fetches,
            known_urls={c.url for c in discovery.candidates} | {c.url for c in discovery.held},
            capture=capture_search,
        )
        _report_search(search_result, card)
        found = _fetch_discovered(
            [
                Candidate(
                    url=c.url,
                    kind=c.kind,
                    attribution="corroborated",
                    basis=c.score.basis if c.score else "found by search",
                    confidence=c.score.confidence if c.score else 0.6,
                    signals=c.score.matched if c.score else [],
                )
                for c in search_result.candidates
                if c.ingestible
            ],
            author,
            cache,
            echo,
            source_notes,
        )
        if found:
            other.extend(found)
            echo(f"  {len(found)} document(s) from verified search sources")
        # Written even when nothing was held: a file that says "nothing was
        # held back" is a result, and its absence would be ambiguous.
        unconfirmed_path = write_unconfirmed(out_dir, card, search_result)
        if search_result.held:
            echo(f"  {len(search_result.held)} candidate(s) written to {unconfirmed_path}")
        echo("")

    if handle and not offline and client is not None:
        # ---- ingest ------------------------------------------------------
        echo("Ingesting (sliding time window):")
        _ACTIVE_LOGGER.context.phase = "ingest"

        resume_seen: dict[str, dict[str, Any]] = {}
        if manifest.raw_tweet_ids and not manifest.ingest_complete:
            # The tweets themselves are in the permanent cache; the manifest
            # only holds their ids. Re-walking would re-pay for search pages,
            # which are billed per tweet returned regardless of what we already
            # have.
            for tid in manifest.raw_tweet_ids:
                cached = cache.get("x", f"tweet:{tid}")
                if cached is not None:
                    resume_seen[tid] = cached
            echo(
                f"  resuming: {len(resume_seen)} of {len(manifest.raw_tweet_ids)} "
                f"previously-ingested posts recovered from cache"
            )

        def _checkpoint(frontier: int, seen: dict[str, dict[str, Any]], st: Any) -> None:
            manifest.until_ts = frontier
            manifest.raw_tweet_ids = list(seen)
            manifest.ingest_stats = st.as_dict()
            manifest.prior_spend = budget.total
            manifest.save(out_dir)

        try:
            raw_tweets, ingest_stats = ingest_timeline(
                client,
                handle,
                since=since_dt,
                max_posts=max_posts,
                window_days=window_days,
                empty_window_tolerance=empty_window_tolerance,
                max_pages=max_pages,
                include_replies=replies,
                probe_enabled=hiatus_probe,
                # statusesCount from the profile, so the report can say
                # "400 of 53,901" instead of leaving it to be inferred.
                public_post_count=public_posts or None,
                resume_until_ts=manifest.until_ts if not manifest.ingest_complete else None,
                resume_seen=resume_seen or None,
                on_progress=_checkpoint,
                log=echo,
            )
        except ProviderError as exc:
            # The load-bearing constraint: a source that dies degrades the
            # run, it does not end it. Every non-X adapter already returns a
            # status instead of raising; X was the one that could still kill
            # a run from inside ingest — and on the run that exposed it, a
            # rate-limited provider discarded 215 free documents from 36
            # other sources.
            #
            # What was already paid for is recoverable: `_checkpoint` has
            # been writing ids into the manifest as the walk went, and every
            # fetched tweet is in the permanent cache. The manifest is
            # deliberately NOT marked ingest_complete, so a later run
            # resumes the walk from the saved frontier.
            from .x.ingest import IngestStats

            x_failure = str(exc)
            raw_tweets = [
                tweet
                for tid in manifest.raw_tweet_ids
                if (tweet := cache.get("x", f"tweet:{tid}")) is not None
            ]
            x_status = "partial" if raw_tweets else "failed"
            ingest_stats = IngestStats(
                fetched=len(raw_tweets),
                unique=len(raw_tweets),
                public_post_count=public_posts or None,
                stop_reason=f"the X provider failed: {x_failure}",
            )
            error(f"X ingestion {x_status}: {x_failure}")
            warn(
                f"recovered {len(raw_tweets)} already-paid-for post(s) from the "
                "checkpoint; continuing on what the other sources produced. A "
                "later run will resume the X walk where this one stopped."
            )
        else:
            cache.put("x", f"corpus:{handle.lower()}", raw_tweets)
            manifest.ingest_complete = True
            manifest.raw_tweet_ids = [t.get("id") or t.get("id_str") or "" for t in raw_tweets]
            manifest.ingest_stats = ingest_stats.as_dict()
            manifest.prior_spend = budget.total
            manifest.save(out_dir)

    if dry_run:
        echo("--dry-run with --offline: nothing to estimate.")
        raise typer.Exit(code=0)

    from .x.hydrate import HydrationStats

    docs: list[Document] = []
    hyd_stats = HydrationStats()
    ingest_meta: dict[str, Any] = {}

    if not handle:
        ingest_meta = {"stop_reason": "no X anchor; this corpus is built from other sources"}
    else:
        ingest_meta = (
            ingest_stats.as_dict()
            if not offline
            else {"stop_reason": "loaded from cache (--offline)"}
        )
        if x_failure:
            # The report's coverage block reads these; a run that lost its X
            # source must say so where the reader decides how much to trust.
            ingest_meta["x_status"] = x_status
            ingest_meta["x_failure"] = x_failure
        share = ingest_meta.get("ingested_share")
        total_known = ingest_meta.get("public_post_count")
        if share is not None and total_known:
            echo(
                f"  {len(raw_tweets)} unique posts of {total_known:,} public "
                f"({share:.1%}) · ${budget.total:.4f} spent"
            )
        else:
            echo(f"  {len(raw_tweets)} unique posts · ${budget.total:.4f} spent")
        echo("")

        if not raw_tweets:
            error("no posts ingested from X.")
            # `other` holds the documents the non-X sources actually produced,
            # which is the question here — candidates that produced nothing
            # cannot carry a run.
            if not other:
                raise typer.Exit(code=1)
            warn("continuing on the other sources alone")

        # ---- hydrate -----------------------------------------------------
        if raw_tweets:
            _ACTIVE_LOGGER.context.phase = "hydrate"
            echo("Hydrating:")
            if client is None:
                provider = _OfflineProvider()
                client = XClient(provider, cache, budget)
            try:
                docs, hyd_stats = hydrate(
                    client,
                    raw_tweets,
                    handle,
                    include_reposts=include_reposts,
                    include_replies=replies,
                    log=echo,
                )
            except (BudgetExceeded, ProviderError) as exc:
                # Un-hydrated documents are still worth keeping — we paid for
                # them. A dying provider is handled like a spent budget here:
                # hydration is a bonus pass, never the reason a run ends —
                # and after a rate-limited ingest it is exactly the next call
                # that would have crashed.
                label = "budget" if isinstance(exc, BudgetExceeded) else "provider"
                echo(f"  [{label}] {exc} — continuing with un-hydrated documents")
                from .x.client import normalize_tweet

                docs = [normalize_tweet(t) for t in raw_tweets]
                hyd_stats = HydrationStats(
                    input_documents=len(raw_tweets),
                    output_documents=len(docs),
                    notes=[f"hydration stopped early ({label}: {exc}); context is missing"],
                )
            echo("")

    manifest.hydrate_complete = True
    manifest.hydrated_documents = len(docs)
    manifest.prior_spend = budget.total
    manifest.save(out_dir)

    if other:
        docs.extend(other)
        docs.sort(key=lambda d: d.published_at, reverse=True)

    if not docs:
        error("no documents from any source. Nothing to synthesize.")
        raise typer.Exit(code=1)

    # ---- signals ---------------------------------------------------------
    _ACTIVE_LOGGER.context.phase = "signals"
    echo("Computing signals (Python, no API calls)...")
    signals = compute_signals(
        docs,
        extra={"ingest": ingest_meta, "hydration": hyd_stats.as_dict()},
        # The subject's own name and handles are boilerplate on their pages,
        # not their vocabulary; drift excludes them.
        subject_terms=[card.name, card.key, handle or ""],
    )
    echo(
        f"  {signals['total_documents']} documents, "
        f"{len(signals['conversation_graph'])} network handles, "
        f"{len(signals['outbound_domains'])} domains, "
        f"{len(signals['vocabulary_drift'])} vocabulary buckets"
    )
    echo("")

    # ---- write corpus + signals before spending on synthesis -------------
    _write_json(out_dir / "corpus.json", [d.model_dump() for d in docs])
    _write_json(out_dir / "signals.json", signals)
    # discovery.json holds the held-back candidates too, so a name-match this
    # run declined to ingest is recorded rather than forgotten.
    _write_json(
        out_dir / "discovery.json",
        {"card": card.as_dict(), **discovery.as_dict(), "search": search_result.as_dict()},
    )
    echo(f"Wrote corpus.json, signals.json and discovery.json to {out_dir}")
    echo("")

    # ---- synthesize ------------------------------------------------------
    run_meta: dict[str, Any] = {
        "ingest": ingest_meta,
        "hydration": hyd_stats.as_dict(),
        "budget_stopped": budget.stopped,
        "discovery": discovery.as_dict(),
        "search": search_result.as_dict(),
        "identity": card.as_dict(),
        "source_notes": source_notes,
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
        _ACTIVE_LOGGER.context.phase = "synthesize"
        echo("Synthesizing (map -> reduce):")
        completed = {int(k): v for k, v in manifest.map_slices.items()}
        if completed:
            echo(f"  {len(completed)} map slice(s) already done; not re-paying for them")

        def _slice_done(index: int, payload: dict[str, Any]) -> None:
            manifest.record_slice(index, payload)
            manifest.prior_spend = budget.total
            manifest.save(out_dir)

        result = asyncio.run(
            synthesize(
                docs,
                signals,
                budget,
                axes=selected_axes,
                map_model=map_model,
                reduce_model=reduce_model,
                map_effort=map_effort,
                reduce_effort=reduce_effort,
                prefilter=not no_filter,
                completed_slices=completed or None,
                on_slice=_slice_done,
                highlights_cap=highlights,
                log=echo,
            )
        )
        manifest.map_total = result.chunks
        manifest.reduce_complete = result.synthesis is not None
        manifest.prior_spend = budget.total
        manifest.save(out_dir)
        synthesis = result.synthesis
        run_meta["synthesis_error"] = result.error
        run_meta["dropped_findings"] = result.dropped_findings
        run_meta["structured_output"] = result.structured_output
        run_meta["corrected_counts"] = result.corrected_counts
        run_meta["filter"] = result.filter_stats.as_dict() if result.filter_stats else {}
        run_meta["analyzed_documents"] = result.analyzed_documents
        run_meta["corpus_tier"] = result.tier.name if result.tier else ""
        run_meta["budget_stopped"] = budget.stopped
        if synthesis is not None:
            _write_json(out_dir / "synthesis.json", synthesis.model_dump())
            with_signal = sum(1 for a in synthesis.axes if a.signal != "none")
            echo(
                f"  wrote synthesis.json ({len(synthesis.core_model)} core beliefs, "
                f"{with_signal}/{len(synthesis.axes)} axes with signal)"
            )
        else:
            if result.raw_reduce_output:
                (out_dir / "reduce_raw_output.txt").write_text(
                    result.raw_reduce_output, encoding="utf-8"
                )
                echo("  dumped unparseable model output to reduce_raw_output.txt")
            error(f"synthesis failed: {result.error}")
            if not budget.stopped:
                exit_code = 1
    echo("")

    # ---- report + spend --------------------------------------------------
    _ACTIVE_LOGGER.context.phase = "render"
    report = render_report(
        handle=author,
        subject=card.display,
        synthesis=synthesis,
        docs=docs,
        signals=signals,
        budget_lines=budget.summary_lines(),
        run_meta=run_meta,
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    # Written so `resynth --render-only` can reproduce the caveat block later
    # without re-deriving anything.
    _write_json(out_dir / "run_meta.json", run_meta)

    # ---- estimate accuracy (3.6) ----------------------------------------
    # An estimator nobody checks is decoration, so every run leaves a row —
    # whether or not anyone runs `corpus budget accuracy` afterwards.
    if not offline and estimated_total is not None:
        cache.log_estimate(
            run_id=budget.run_id,
            handle=card.key,
            category="total",
            estimated=estimated_total,
            actual=budget.this_attempt,
            posts_estimated=estimated_posts or 0,
            posts_actual=len(raw_tweets),
            note="skip-synthesis" if skip_synthesis else "",
        )
        if estimated_total > 0:
            err = budget.this_attempt / estimated_total - 1
            echo(
                f"  estimate ${estimated_total:.4f} vs actual "
                f"${budget.this_attempt:.4f} ({err:+.0%})"
            )
    for note in budget.estimate_misses:
        warn(note)

    echo("Spend:")
    for line in budget.summary_lines():
        echo(f"  {line}")
    echo("")
    echo(f"Report: {out_dir / 'report.md'}")

    if budget.stopped:
        echo("")
        warn(
            "the budget was exhausted. Results are partial, but every paid byte "
            "was written to disk. Re-run with a higher --budget to continue."
        )
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


def _resolve_card(
    *,
    target: str | None,
    profiles: Path | None,
    name: str = "",
    employer: str = "",
    role: str = "",
    location: str = "",
    x: str = "",
    github: str = "",
    site: str = "",
    substack: str = "",
    bluesky: str = "",
    hn: str = "",
    reddit: str = "",
    mastodon: str = "",
) -> IdentityCard:
    """Who this run is about: a saved target, flags, or both.

    A flag always beats the file, and never writes back to it — an override on
    the command line is not a decision to edit the user's notes.
    """
    flags = {
        "name": name,
        "employer": employer,
        "role": role,
        "location": location,
        "x": x,
        "github": github,
        "site": site,
        "substack": substack,
        "bluesky": bluesky,
        "hn": hn,
        "reddit": reddit,
        "mastodon": mastodon,
    }
    if target:
        return merge_flags(load_target(target, profiles), **flags)
    return build_card(
        name=name,
        employer=employer,
        role=role,
        location=location,
        x=x,
        github=github,
        site=site,
        substack=substack,
        bluesky=bluesky,
        hn=hn,
        reddit=reddit,
        mastodon=mastodon,
    )


def _anchor_line(card: IdentityCard) -> str:
    if not card.anchors:
        return "none — nothing to read"
    return " · ".join(f"{kind}:{value}" for kind, value in sorted(card.anchors.items()))


def _report_discovery(result: DiscoveryResult, card: IdentityCard, *, following: bool) -> None:
    """What discovery found, at a glance, before anything is paid for."""
    if not following:
        echo(f"Discovery: --no-discover, reading the {len(result.candidates)} anchor(s) only.")
        return
    mix = result.by_attribution()
    echo(
        f"Discovery (phase 1, link-following): {len(result.candidates)} source(s) "
        f"from {result.fetches} fetch(es) and {result.cached_fetches} cache hit(s)"
    )
    if mix:
        echo("  " + ", ".join(f"{count} {label}" for label, count in sorted(mix.items())))
    for candidate in result.candidates:
        marker = "" if candidate.ingestible else "  [no adapter yet]"
        echo(f"  {candidate.attribution:<12} {candidate.url}  ({candidate.basis}){marker}")
    for signal in result.identity_signals[:5]:
        echo(f"  signal: {signal}")
    if result.held:
        warn(
            f"{len(result.held)} candidate(s) matched the name and nothing else. They "
            "are NOT in this corpus; see discovery.json."
        )
        for candidate in result.held[:5]:
            echo(f"  held: {candidate.url} — missing: {', '.join(candidate.missing)}")
    for note in result.notes[:5]:
        echo(f"  note: {note}")
    for problem in result.errors[:5]:
        warn(f"discovery: {problem}")


# Which adapter reads which kind of find. A kind with no adapter is still
# reported as "found it, cannot read it yet" rather than dropped — see
# Candidate.ingestible.
def _fetch_one(
    kind: str, url: str, author: str, cache: Cache, log: Any, notes: list[str]
) -> list[Document]:
    """Read one source, or raise SourceError. Never anything else.

    The net is deliberately wide. Adapters wrap HTTP status codes in
    SourceError but not transport failures — a DNS miss, a TLS error, a proxy
    403 — and those arrive as httpx exceptions that no caller was catching. A
    dead feed must cost the corpus some documents and cost the run nothing,
    which is the rule every source in this tool follows.
    """
    from .sources.base import SourceError
    from .sources.bluesky import BlueskySource
    from .sources.github import GitHubSource
    from .sources.hackernews import HackerNewsSource
    from .sources.mastodon import MastodonSource
    from .sources.reddit import RedditSource
    from .sources.rss import RSSSource
    from .sources.substack import SubstackSource
    from .sources.web import WebSource

    try:
        if kind == "rss":
            return RSSSource().fetch(url, author_handle=author, cache=cache, log=log)
        if kind == "substack":
            domain = url.removeprefix("https://").removeprefix("http://").strip("/")
            return SubstackSource().fetch(domain, author_handle=author, cache=cache, log=log)
        if kind == "github":
            login = url.rstrip("/").rsplit("/", 1)[-1]
            # GitHub is the one adapter with coverage limits worth stating in
            # the report: its events feed reaches ~90 days, and the search path
            # that reaches further has its own per-minute rate limit. Those
            # notes travel with the documents rather than being logged and lost.
            docs, stats = GitHubSource().fetch_with_stats(
                login, author_handle=author, cache=cache, log=log
            )
            notes.extend(f"GitHub: {note}" for note in stats.notes)
            return docs
        if kind == "bluesky":
            return BlueskySource().fetch(url, author_handle=author, cache=cache, log=log)
        if kind == "hn":
            return HackerNewsSource().fetch(url, author_handle=author, cache=cache, log=log)
        if kind == "reddit":
            return RedditSource().fetch(url, author_handle=author, cache=cache, log=log)
        if kind == "mastodon":
            return MastodonSource().fetch(url, author_handle=author, cache=cache, log=log)
        return WebSource().fetch(url, author_handle=author, cache=cache, log=log)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"{kind} {url}: {exc}") from exc


def _fetch_discovered(
    candidates: list[Candidate], author: str, cache: Cache, log: Any, notes: list[str]
) -> list[Document]:
    """Read every source discovery is confident enough to ingest.

    Non-fatal throughout, like every other source: a feed that 404s costs the
    corpus some documents and costs the run nothing.
    """
    docs: list[Document] = []
    if not candidates:
        return docs
    from .sources.base import SourceError, attribute

    log("Reading discovered sources:")
    for candidate in candidates:
        if not candidate.ingestible:
            log(f"  {candidate.url}: no adapter for '{candidate.kind}' yet, skipped")
            continue
        try:
            found = _fetch_one(candidate.kind, candidate.url, author, cache, log, notes)
        except SourceError as exc:
            log(f"  {candidate.url} failed (non-fatal): {exc}")
            continue
        docs.extend(
            attribute(
                found,
                attribution=candidate.attribution,
                basis=candidate.basis,
                confidence=candidate.confidence,
            )
        )
    return docs


def _run_search(
    card: IdentityCard,
    cache: Cache,
    budget: Budget,
    *,
    max_searches: int,
    max_fetches: int,
    known_urls: set[str],
    capture: Path | None,
) -> SearchPhaseResult:
    """Phase 2, wrapped so it can never be the reason a run dies.

    Search is billable and optional. A missing key, an unimplemented provider,
    or a budget that will not cover a query all degrade to "the phase did not
    run" with the reason attached — the corpus is thinner and the report says
    why.
    """
    try:
        provider = get_search_provider(
            capture=SearchCapture(capture, log=echo) if capture is not None else None,
            log=echo,
        )
    except (SearchError, NotImplementedError) as exc:
        warn(f"search did not run: {exc}")
        return SearchPhaseResult(notes=[f"search did not run: {exc}"])

    if capture is not None:
        echo(f"  [capture] raw search responses -> {capture}")
    client = SearchClient(provider, cache, budget, log=echo)
    try:
        return search_for_sources(
            card,
            cache,
            client,
            max_searches=max_searches,
            max_fetches=max_fetches,
            known_urls=known_urls,
            log=echo,
        )
    except BudgetExceeded as exc:
        warn(f"search stopped: {exc}")
        return SearchPhaseResult(notes=[f"search stopped: {exc}"])
    finally:
        client.close()


def _report_search(result: SearchPhaseResult, card: IdentityCard) -> None:
    """What search did, at a glance, in the order a reader needs it."""
    echo(
        f"Discovery (phase 2, search): {result.searches_run} search(es) "
        f"({result.cached_searches} cached), {result.results_seen} result(s), "
        f"{result.verified_count} verified"
    )
    if result.unread:
        # Same class of misleading output the live checker now refuses to
        # print: every candidate was scored on a search result alone, so the
        # held count says nothing about them.
        warn(
            f"no candidate page could be read ({result.reads_attempted} attempted). "
            "Nothing from search can be corroborated without reading the page, so "
            "everything found is held — check the errors below rather than the counts."
        )
    if result.common_name:
        # The stop-and-ask path. Loud, because the alternative to saying this
        # is guessing which of several people the subject is.
        warn(
            f"independent pages attach conflicting identity facts to "
            f"'{card.display}'. Nothing from search was ingested."
        )
        fields = ", ".join(f"--{f}" for f in result.disambiguators)
        if fields:
            echo(f"  Adding {fields} would narrow it more than any extra searching.")
    for candidate in result.candidates:
        echo(
            f"  corroborated {candidate.url}  ({candidate.score.basis if candidate.score else ''})"
        )
    if result.held:
        warn(
            f"{len(result.held)} candidate(s) were not confirmed as theirs. They are NOT "
            "in this corpus; see unconfirmed.md."
        )
    if result.context:
        echo(
            f"  {len(result.context)} page(s) are about them rather than by them, and are "
            "recorded as context only"
        )
    if result.rejected:
        echo(f"  {len(result.rejected)} result(s) rejected outright (aggregators, excludes)")
    for note in result.notes[:4]:
        echo(f"  note: {note}")
    for problem in result.errors[:4]:
        warn(f"search: {problem}")


def _accept_unconfirmed(
    path: Path,
    card: IdentityCard,
    profiles: Path | None,
    target: str | None,
    *,
    assume_yes: bool,
) -> tuple[IdentityCard, list[str]]:
    """Read back an edited unconfirmed.md and act on both halves of it.

    Checked entries are returned for ingestion. Unchecked ones are written
    into the card's `exclude` list, which is the part that needs a guard: an
    unedited file has nothing checked, and acting on it literally would reject
    every candidate forever on the strength of a file nobody read.
    """
    entries = read_unconfirmed(path)
    if not entries:
        raise IdentityError(f"no checklist entries found in {path}")

    accepted = [e.url for e in entries if e.checked]
    rejected = [e.url for e in entries if not e.checked]
    echo(f"Reading {path}: {len(accepted)} accepted, {len(rejected)} rejected")

    if not accepted:
        warn(
            f"nothing is ticked in {path}, so all {len(rejected)} candidate(s) would be "
            "excluded permanently. That is what an unedited file looks like."
        )
        if not assume_yes and not typer.confirm("Exclude all of them anyway?", default=False):
            echo("Leaving the file alone; nothing accepted and nothing excluded.")
            return card, []

    card.exclude = list(dict.fromkeys([*card.exclude, *rejected]))
    if target and rejected:
        # Only when the card came from the file. An ad-hoc card has no entry
        # to update, and creating one as a side effect of a flag would be a
        # surprising way to acquire a saved target.
        written = save_target(card, profiles)
        echo(f"  {len(rejected)} rejected URL(s) added to `exclude` in {written}")
    elif rejected:
        echo(
            f"  {len(rejected)} rejected URL(s) apply to this run only — this card is not "
            "saved. Use `corpus profile --target KEY --exclude URL` to keep them."
        )
    return card, accepted


def _fetch_accepted(
    urls: list[str],
    author: str,
    cache: Cache,
    log: Any,
    notes: list[str],
) -> list[Document]:
    """Sources a human ticked in unconfirmed.md.

    Attributed `corroborated` with basis `user-confirmed`: a person saying
    "yes, this is them" is better evidence than any signal in the scorer, but
    it is still not a URL they supplied as an anchor, so it does not get
    anchor's certainty.
    """
    docs: list[Document] = []
    if not urls:
        return docs
    from .sources.base import SourceError, attribute

    log("Sources confirmed by hand:")
    for entry in urls:
        try:
            found = _fetch_one(kind_for(entry), entry, author, cache, log, notes)
        except SourceError as exc:
            log(f"  {entry} failed (non-fatal): {exc}")
            continue
        docs.extend(
            attribute(
                found,
                attribution="corroborated",
                basis="user-confirmed from unconfirmed.md",
                confidence=0.75,
            )
        )
    return docs


def _fetch_secondary(
    handle: str,
    rss: list[str],
    urls: list[str],
    cache: Cache,
    log: Any,
    notes: list[str],
) -> list[Document]:
    """`--rss` and `--url`: URLs the user typed, so anchor-attributed.

    They stay direct source flags rather than becoming card anchors because
    both are repeatable and neither is crawled — "read this page" and "this
    person owns this domain" are different statements.
    """
    docs: list[Document] = []
    if not (rss or urls):
        return docs
    log("Sources named on the command line:")
    from .sources.base import SourceError, attribute

    for kind, targets in (("rss", rss), ("web", urls)):
        for entry in targets:
            try:
                found = _fetch_one(kind, entry, handle, cache, log, notes)
            except SourceError as exc:
                log(f"  {kind} {entry} failed (non-fatal): {exc}")
                continue
            docs.extend(
                attribute(found, attribution="anchor", basis=f"--{kind} on the command line")
            )
    return docs


# --------------------------------------------------------------------------


# Fields that only ever existed in the pre-cognition schema. Their presence is
# how a stale synthesis.json is recognised without guessing from a validation
# error, which could equally mean a truncated file.
LEGACY_SYNTHESIS_FIELDS = ("themes", "hooks", "performance_gap", "reading_diet")

MIGRATION_HINT = (
    "synthesis.json was produced by the pre-cognition schema (found: {found}).\n"
    "  It cannot be re-rendered — the new report needs fields the old run never\n"
    "  produced. corpus.json is unchanged and still valid, so re-run\n"
    "  `corpus resynth {directory}` to regenerate synthesis.json under the new\n"
    "  schema. That costs Anthropic tokens but no X spend: nothing is re-fetched."
)


def load_synthesis(path: Path) -> Synthesis:
    """Read a synthesis.json, or fail with a migration message you can act on."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        found = [f for f in LEGACY_SYNTHESIS_FIELDS if f in payload]
        if found and "core_model" not in payload:
            raise ValueError(MIGRATION_HINT.format(found=", ".join(found), directory=path.parent))
    return Synthesis.model_validate(payload)


def _render_only(
    directory: Path, handle: str, docs: list[Document], signals: dict[str, Any]
) -> None:
    """Regenerate report.md from what is already on disk. No client, no spend.

    This exists so iterating on the report's shape is free. A formatting change
    that costs a reduce call is a formatting change you do not make.
    """
    synthesis_path = directory / "synthesis.json"
    if not synthesis_path.exists():
        error(f"--render-only needs {synthesis_path}, which does not exist.")
        echo(f"  Run `corpus resynth {directory}` first to produce it.")
        raise typer.Exit(code=2)

    try:
        synthesis = load_synthesis(synthesis_path)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    meta_path = directory / "run_meta.json"
    run_meta: dict[str, Any] = {
        "ingest": signals.get("ingest", {}),
        "hydration": signals.get("hydration", {}),
    }
    if meta_path.exists():
        run_meta.update(json.loads(meta_path.read_text(encoding="utf-8")))

    report = render_report(
        handle=handle,
        synthesis=synthesis,
        docs=docs,
        signals=signals,
        budget_lines=["(--render-only: no API calls, nothing spent)"],
        run_meta=run_meta,
    )
    (directory / "report.md").write_text(report, encoding="utf-8")
    echo(f"Re-rendered {directory / 'report.md'} from synthesis.json — $0.0000 spent.")
    raise typer.Exit(code=0)


@app.command()
def resynth(
    directory: Path = typer.Argument(..., help="An existing out/<handle>/<date> directory."),
    budget_limit: float = typer.Option(10.00, "--budget"),
    axes: str | None = typer.Option(None, "--axes"),
    map_model: str = typer.Option(MAP_MODEL, "--map-model"),
    reduce_model: str = typer.Option(REDUCE_MODEL, "--reduce-model"),
    map_effort: str = typer.Option("medium", "--map-effort"),
    reduce_effort: str = typer.Option("high", "--reduce-effort"),
    highlights: int = typer.Option(
        DEFAULT_HIGHLIGHTS,
        "--highlights",
        help="Complete documents pasted into the reduce prompt. The biggest cost "
        "lever in the tool: ~60% of a real run's spend at the default.",
    ),
    no_filter: bool = typer.Option(False, "--no-filter"),
    render_only: bool = typer.Option(
        False,
        "--render-only",
        help="Rebuild report.md from the existing synthesis.json. Zero API calls.",
    ),
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

    if render_only:
        _render_only(directory, handle, docs, signals)
        return

    try:
        selected_axes = select_axes(axes)
    except AxisError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    cache = Cache()
    budget = Budget(limit=budget_limit, cache=cache)
    echo(f"resynth @{handle}: {len(docs)} documents, no fetching (run {budget.run_id})")
    echo(f"  axes: {', '.join(a.name for a in selected_axes)}")
    echo("")

    # The run directory's manifest holds every map slice a previous attempt
    # completed — the X walk's checkpoint discipline, applied to synthesis.
    # "Budget stop, raise budget, re-run" is the most common workflow this
    # tool has, and it used to re-bill the entire map phase on every lap:
    # the simonw-nox run paid $0.61 for 15 slices and then $0.63 to run the
    # same 15 again. Slices are validated against the current chunking inside
    # `synthesize`, so a changed corpus or filter re-runs rather than reuses.
    manifest = RunManifest.load(directory) or RunManifest(handle=handle, run_id=budget.run_id)
    completed = {int(k): v for k, v in manifest.map_slices.items()}
    if completed:
        echo(f"  {len(completed)} map slice(s) already done; not re-paying for them")

    def _slice_done(index: int, payload: dict[str, Any]) -> None:
        manifest.record_slice(index, payload)
        manifest.prior_spend = budget.total
        manifest.save(directory)

    result = asyncio.run(
        synthesize(
            docs,
            signals,
            budget,
            axes=selected_axes,
            map_model=map_model,
            reduce_model=reduce_model,
            map_effort=map_effort,
            reduce_effort=reduce_effort,
            prefilter=not no_filter,
            completed_slices=completed or None,
            on_slice=_slice_done,
            highlights_cap=highlights,
            log=echo,
        )
    )
    manifest.map_total = result.chunks
    manifest.reduce_complete = result.synthesis is not None
    manifest.prior_spend = budget.total
    manifest.save(directory)
    run_meta = {
        "ingest": signals.get("ingest", {}),
        "hydration": signals.get("hydration", {}),
        "synthesis_error": result.error,
        "dropped_findings": result.dropped_findings,
        "corrected_counts": result.corrected_counts,
        "structured_output": result.structured_output,
        "filter": result.filter_stats.as_dict() if result.filter_stats else {},
        "analyzed_documents": result.analyzed_documents,
        "corpus_tier": result.tier.name if result.tier else "",
        "budget_stopped": budget.stopped,
    }

    if result.synthesis is not None:
        _write_json(directory / "synthesis.json", result.synthesis.model_dump())
        _write_json(directory / "run_meta.json", run_meta)
    elif result.raw_reduce_output:
        (directory / "reduce_raw_output.txt").write_text(result.raw_reduce_output, encoding="utf-8")
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


@app.command()
def profile(
    target: str | None = typer.Option(None, "--target", help="Show or update one saved target."),
    key: str | None = typer.Option(None, "--key", help="Target key. Defaults to a slug of --name."),
    name: str = typer.Option("", "--name"),
    employer: str = typer.Option("", "--employer"),
    role: str = typer.Option("", "--role"),
    location: str = typer.Option("", "--location", help="Disambiguates a common name."),
    x: str = typer.Option("", "--x", help="X handle. An anchor."),
    github: str = typer.Option("", "--github", help="GitHub username. An anchor."),
    site: str = typer.Option("", "--site", help="Their own site. An anchor."),
    substack: str = typer.Option("", "--substack", help="Substack domain. An anchor."),
    rss: str = typer.Option("", "--rss", help="Feed URL. An anchor."),
    bluesky: str = typer.Option("", "--bluesky", help="Bluesky handle. An anchor."),
    hn: str = typer.Option("", "--hn", help="Hacker News username. An anchor."),
    reddit: str = typer.Option("", "--reddit", help="Reddit username. An anchor."),
    mastodon: str = typer.Option("", "--mastodon", help="@user@instance. An anchor."),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="A known false positive. Repeatable, and never ingested."
    ),
    profiles: Path | None = typer.Option(None, "--profiles", metavar="PATH"),
) -> None:
    """Create, update, or show the identity card a run is scored against.

    With no arguments it lists what is on file. Anchors are what make discovery
    safe: everything found later is scored against them, and a run with no
    anchors has nothing to check a name match against.
    """
    path = profiles or default_profiles_path()
    flags = {
        "name": name,
        "employer": employer,
        "role": role,
        "location": location,
        "x": x,
        "github": github,
        "site": site,
        "substack": substack,
        "rss": rss,
        "bluesky": bluesky,
        "hn": hn,
        "reddit": reddit,
        "mastodon": mastodon,
    }
    writing = any(flags.values()) or bool(exclude)

    try:
        if not writing:
            if target:
                _show_card(load_target(target, path), path)
                return
            cards = load_targets(path)
            if not cards:
                echo(f"no targets in {path}")
                echo('  corpus profile --name "Jane Smith" --employer "Acme" --github jsmith')
                return
            echo(f"{len(cards)} target(s) in {path}:")
            for card in cards.values():
                echo(f"  {card.key:<16} {card.display:<28} {_anchor_line(card)}")
            return

        if target:
            card = merge_flags(load_target(target, path), **flags)
            card.exclude = list(dict.fromkeys([*card.exclude, *exclude]))
        else:
            card = build_card(
                key=key or "",
                name=name,
                employer=employer,
                role=role,
                location=location,
                x=x,
                github=github,
                site=site,
                substack=substack,
                rss=rss,
                bluesky=bluesky,
                hn=hn,
                reddit=reddit,
                mastodon=mastodon,
                exclude=list(exclude),
            )
    except IdentityError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    written = save_target(card, path)
    echo(f"wrote target '{card.key}' to {written}")
    _show_card(card, written)
    if not card.anchors:
        warn(
            "this card has no anchors, so discovery has nothing to follow and "
            "nothing to score a name match against. Add --x, --github, --site, "
            "--substack, --bluesky, --hn, --reddit, --mastodon, or --rss."
        )


def _show_card(card: IdentityCard, path: Path) -> None:
    echo(f"target {card.key} ({path})")
    for label in ("name", "employer", "role", "location"):
        value = getattr(card, label)
        if value:
            echo(f"  {label + ':':<10} {value}")
    echo(f"  {'anchors:':<10} {_anchor_line(card)}")
    for entry in card.exclude:
        echo(f"  {'exclude:':<10} {entry}")
    echo("")
    echo(f"  corpus run --target {card.key}")


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


@cache_app.command("vacuum")
def cache_vacuum() -> None:
    """Reclaim space and checkpoint the write-ahead log."""
    cache = Cache()
    result = cache.vacuum()
    echo(f"path:      {cache.path}")
    echo(f"before:    {result['before_bytes'] / 1024:.1f} KiB")
    echo(f"after:     {result['after_bytes'] / 1024:.1f} KiB")
    echo(f"reclaimed: {result['reclaimed_bytes'] / 1024:.1f} KiB")
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


@budget_app.command("accuracy")
def budget_accuracy(limit: int = typer.Option(50, "--limit")) -> None:
    """How wrong --dry-run has been, historically.

    Reports signed error so a systematic bias is visible: an estimator that is
    consistently 30% low is a different problem from one that is noisy, and
    only the first one will surprise you at the top of a budget.
    """
    cache = Cache()
    rows = cache.estimate_log(limit=limit)
    if not rows:
        echo("no estimates recorded yet — run `corpus run` at least once")
        cache.close()
        return

    echo(f"{'when':<17} {'handle':<16} {'est':>9} {'actual':>9} {'error':>8} {'posts':>13}")
    echo("-" * 78)
    errors: list[float] = []
    for row in rows:
        when = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        est, actual = row["estimated"], row["actual"]
        err = (actual / est - 1) if est else 0.0
        errors.append(err)
        posts = f"{row['posts_actual']}/{row['posts_estimated']}"
        echo(
            f"{when:<17} {row['handle'][:16]:<16} ${est:>8.4f} ${actual:>8.4f} "
            f"{err:>+7.0%} {posts:>13}"
        )

    echo("")
    mean = sum(errors) / len(errors)
    absolute = sum(abs(e) for e in errors) / len(errors)
    worst = max(errors, key=abs)
    echo(f"runs:          {len(errors)}")
    echo(f"mean error:    {mean:+.1%}  (bias: estimates run {'low' if mean > 0 else 'high'})")
    echo(f"mean |error|:  {absolute:.1%}  (spread, regardless of direction)")
    echo(f"worst:         {worst:+.0%}")
    if absolute > 0.30:
        echo("")
        echo("The estimator is off by more than 30% on average. The assumptions")
        echo("live in estimate_x_cost/estimate_anthropic_cost in corpus/budget.py:")
        echo("hydration ratio, tokens per document, and map chunk size.")
    cache.close()


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Loopback only. Anything else is refused: this serves without auth.",
    ),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Local web interface: form, dry-run gate, queue, history, audit log.

    A thin wrapper over this CLI — every run it starts is a `corpus run`
    with the same budget pre-flight, the same `.env`, and the same output,
    streamed to the browser. Binds to the loopback interface only; there is
    no authentication, deliberately, because there is no network exposure.
    """
    from .web.app import create_app, ensure_loopback
    from .web.store import WebStore, default_web_db_path

    try:
        ensure_loopback(host)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    import uvicorn

    store = WebStore()
    echo(f"corpus web interface: http://{host}:{port}/")
    echo(f"  queue and audit database: {default_web_db_path()}")
    echo("  local only — not reachable from the network. Ctrl-C stops it.")
    uvicorn.run(create_app(store), host=host, port=port, log_level="warning")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        echo("\ninterrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
