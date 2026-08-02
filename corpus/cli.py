"""corpus — CLI.

corpus run --x paulg
corpus run --x paulg --max-posts 5000 --since 2020-01-01 --budget 15
corpus run --x someone --dry-run
corpus run --x someone --also-substack example.com
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

from .budget import (
    BUDGET_MODES,
    STRICT,
    Budget,
    BudgetExceeded,
    estimate_anthropic_cost,
    estimate_x_cost,
)
from .cache import DEFAULT_TTL_SECONDS, Cache
from .logging_setup import LOG_FORMATS, TEXT, RunLogger
from .manifest import RunManifest
from .axes import AxisError, select_axes
from .models import Document, Synthesis
from .render import render_report
from .synthesize import MAP_MODEL, REDUCE_MODEL, synthesize
from .x.capture import RawCapture
from .x.client import XClient
from .x.hydrate import hydrate
from .x.ingest import DEFAULT_EMPTY_WINDOW_TOLERANCE, ingest_timeline
from .x.providers import ProviderError, get_provider
from .x.signals import compute_signals
from .x.validate import InvalidHandle, validate_handle

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
    """Ingest, hydrate, and synthesize one person's public writing."""
    if log_format not in LOG_FORMATS:
        typer.echo(f"ERROR: --log-format must be one of {', '.join(LOG_FORMATS)}")
        raise typer.Exit(code=2)
    # Validate at the boundary, where the error can name the input and the user
    # can still fix it. An unvalidated handle does not fail downstream — it
    # silently changes what the search query means.
    try:
        handle = validate_handle(x)
    except InvalidHandle as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc
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
        if manifest.handle and manifest.handle != handle:
            error(f"--resume {resume} is a run for @{manifest.handle}, not @{handle}")
            raise typer.Exit(code=2)
        # A resumed run's budget covers everything the target has cost, not
        # just this attempt — otherwise --budget 10 resumed three times is a $30
        # run, and the flag documented as a hard stop would be per-attempt.
        budget.prior_spend = manifest.prior_spend

    echo(f"corpus run @{handle} (run {budget.run_id})")
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
    out_dir = Path(resume) if resume else _out_dir(out, handle)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest or RunManifest(handle=handle, run_id=budget.run_id)
    manifest.handle = manifest.handle or handle
    manifest.run_id = budget.run_id

    # ---- estimate + confirm ---------------------------------------------
    estimated_total: float | None = None
    estimated_posts: int = 0
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
        target = min(max_posts, public_posts) if public_posts else max_posts
        x_cost = estimate_x_cost(target)
        llm_cost = 0.0 if skip_synthesis else estimate_anthropic_cost(target)

        estimated_total = x_cost + llm_cost
        estimated_posts = target
        echo(f"  @{handle}: {public_posts or 'unknown'} public posts on file")
        echo(f"  estimate: ~{target} posts")
        echo(f"    X data (twitterapi.io): ~${x_cost:.3f}")
        echo(f"    Anthropic synthesis:    ~${llm_cost:.3f}")
        echo(f"    total:                  ~${x_cost + llm_cost:.3f} of ${budget_limit:.2f} budget")
        if x_cost + llm_cost > budget_limit:
            warn("the estimate exceeds the budget; the run will stop early")
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
        cache.put("x", f"corpus:{handle.lower()}", raw_tweets)
        manifest.ingest_complete = True
        manifest.raw_tweet_ids = [t.get("id") or t.get("id_str") or "" for t in raw_tweets]
        manifest.ingest_stats = ingest_stats.as_dict()
        manifest.prior_spend = budget.total
        manifest.save(out_dir)

    if dry_run:
        echo("--dry-run with --offline: nothing to estimate.")
        raise typer.Exit(code=0)

    ingest_meta = (
        ingest_stats.as_dict() if not offline else {"stop_reason": "loaded from cache (--offline)"}
    )
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
        error("no posts ingested. Nothing to synthesize.")
        raise typer.Exit(code=1)

    # ---- hydrate ---------------------------------------------------------
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

    manifest.hydrate_complete = True
    manifest.hydrated_documents = len(docs)
    manifest.prior_spend = budget.total
    manifest.save(out_dir)

    # ---- secondary sources ----------------------------------------------
    secondary = _fetch_secondary(handle, substack, rss, url, cache, echo)
    if secondary:
        docs.extend(secondary)
        docs.sort(key=lambda d: d.published_at, reverse=True)
        echo("")

    # ---- signals ---------------------------------------------------------
    _ACTIVE_LOGGER.context.phase = "signals"
    echo("Computing signals (Python, no API calls)...")
    signals = compute_signals(docs, extra={"ingest": ingest_meta, "hydration": hyd_stats.as_dict()})
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
        handle=handle,
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
            handle=handle,
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
            log=echo,
        )
    )
    run_meta = {
        "ingest": signals.get("ingest", {}),
        "hydration": signals.get("hydration", {}),
        "synthesis_error": result.error,
        "dropped_findings": result.dropped_findings,
        "corrected_counts": result.corrected_counts,
        "structured_output": result.structured_output,
        "filter": result.filter_stats.as_dict() if result.filter_stats else {},
        "analyzed_documents": result.analyzed_documents,
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


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        echo("\ninterrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
