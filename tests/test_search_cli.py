"""Phase 2 through the actual CLI.

The end-to-end question this file answers: does a run that searches put only
verified sources in the corpus, and does everything else end up somewhere a
person can act on it?

Offline throughout. The search provider is a fake, the HTTP client raises if
anything reaches for it, and every page the verification pass could fetch is
pre-seeded in the cache.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest
from fake_anthropic import FakeAnthropic
from typer.testing import CliRunner

from corpus import cli as cli_module
from corpus import discovery as discovery_module
from corpus.cache import Cache
from corpus.cli import app
from corpus.search.providers import SearchResult, SearchUsage

runner = CliRunner()
HAIKU = "claude-haiku-4-5-20251001"

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Jane</title>
<item><title>Essay</title><link>https://janesmith.com/e1</link>
<pubDate>Tue, 12 Mar 2024 10:00:00 +0000</pubDate>
<description>Rubrics beat interviews, and here is the argument in full.</description>
<guid>https://janesmith.com/e1</guid></item>
</channel></rss>"""

# Verified: declares the author, names the employer, links to the anchor.
THEIRS = (
    '<html><head><title>On rubrics</title><meta name="author" content="Jane Smith">'
    "</head><body><p>Rubrics beat interviews. Written at Acme Corp, at some length, "
    "because the argument needs the room to work.</p>"
    '<a href="https://github.com/jsmith">code</a></body></html>'
)
# A different Jane Smith: byline matches, employer contradicts.
SOMEONE_ELSE = (
    '<html><head><meta name="author" content="Jane Smith"></head><body><p>'
    "Jane Smith is a partner at Beta Industries and writes here about tax law "
    "at considerable length.</p>"
    '<a href="https://github.com/jsmith">g</a></body></html>'
)


class ScriptedProvider:
    """A search vendor with a fixed answer, and no way out of the process."""

    name = "scripted"

    def __init__(self, hits: list[SearchResult]) -> None:
        self.hits = hits
        self.queries: list[str] = []
        self.model = HAIKU
        self.last_usage = SearchUsage(model=HAIKU)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        self.queries.append(query)
        self.last_usage = SearchUsage(searches=1, input_tokens=800, output_tokens=10, model=HAIKU)
        # Only the first query returns anything, so a test can reason about
        # candidates without caring which query found them.
        return self.hits[:limit] if len(self.queries) == 1 else []

    def close(self) -> None:
        return None


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CORPUS_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("CORPUS_PROFILES", str(tmp_path / "profiles.yaml"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-by-the-fake")
    monkeypatch.setattr(
        cli_module, "synthesize", partial(cli_module.synthesize, client=FakeAnthropic())
    )
    monkeypatch.setattr(cli_module, "_ACTIVE_LOGGER", None)

    def forbidden(*args, **kwargs):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(discovery_module, "http_client", forbidden)

    state: dict = {
        "out": tmp_path / "out",
        "cache": tmp_path / "cache.db",
        "profiles": tmp_path / "profiles.yaml",
        "provider": None,
    }

    def use(hits: list[SearchResult]):
        provider = ScriptedProvider(hits)
        state["provider"] = provider
        monkeypatch.setattr(cli_module, "get_search_provider", lambda **_: provider)
        return provider

    state["use"] = use
    use([])
    return state


def seed(wired, entries: dict[str, str], source: str = "discovery") -> None:
    cache = Cache(path=wired["cache"])
    for key, body in entries.items():
        cache.put(source, key, body)
    cache.close()


def seed_base(wired, extra_pages: dict[str, str] | None = None) -> None:
    """Everything phase 1 reaches, plus whatever a test adds.

    Each adapter caches under its own source key, so a page has to be seeded
    where its reader will look for it — `discovery` for the crawl, `rss` and
    `web` for the adapters that turn a URL into documents.
    """
    seed(wired, {**BASE_SEED, **{f"get:{u}": b for u, b in (extra_pages or {}).items()}})
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")
    for url, body in (extra_pages or {}).items():
        seed(wired, {f"web:{url}": body}, source="web")


def invoke(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def outputs(base: Path, key: str) -> Path:
    days = sorted((base / key).iterdir())
    assert days, f"no output directory under {base / key}"
    return days[-1]


def run_with(wired, *extra: str):
    return invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--employer",
            "Acme Corp",
            "--github",
            "jsmith",
            "--site",
            "https://janesmith.com",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            *extra,
        ]
    )


BASE_SEED = {
    "get:https://api.github.com/users/jsmith": json.dumps({"blog": "https://janesmith.com"}),
    "get:https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md": "hello",
    "get:https://janesmith.com": (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        'href="/feed.xml"></head><body>hi</body></html>'
    ),
    "get:https://janesmith.com/feed.xml": FEED,
}


# -- the corpus gets only what was verified ---------------------------------


def test_a_verified_source_is_ingested_and_marked_corroborated(wired) -> None:
    wired["use"]([SearchResult(url="https://thinking.example/rubrics", snippet="Jane Smith")])
    seed_base(wired, {"https://thinking.example/rubrics": THEIRS})

    result = run_with(wired)
    assert result.exit_code == 0, result.output

    corpus = json.loads((outputs(wired["out"], "janesmith") / "corpus.json").read_text())
    found = [d for d in corpus if "thinking.example" in d["url"]]
    assert found, "the verified source never reached the corpus"
    assert found[0]["attribution"] == "corroborated"
    assert "found by search" in found[0]["attribution_basis"]


def test_a_page_that_contradicts_the_card_stays_out_of_the_corpus(wired) -> None:
    """The failure the whole phase exists to prevent, end to end."""
    wired["use"]([SearchResult(url="https://taxblog.example/post", snippet="Jane Smith")])
    seed_base(wired, {"https://taxblog.example/post": SOMEONE_ELSE})

    result = run_with(wired)
    assert result.exit_code == 0, result.output

    out = outputs(wired["out"], "janesmith")
    corpus = json.loads((out / "corpus.json").read_text())
    assert not any("taxblog" in d["url"] for d in corpus)
    assert "taxblog.example" in (out / "unconfirmed.md").read_text()


def test_held_candidates_are_written_to_unconfirmed_md(wired) -> None:
    wired["use"]([SearchResult(url="https://maybe.example/post", snippet="Jane Smith wrote this")])
    seed_base(wired)

    result = run_with(wired)
    assert result.exit_code == 0, result.output

    text = (outputs(wired["out"], "janesmith") / "unconfirmed.md").read_text()
    assert "- [ ] https://maybe.example/post" in text
    assert "Found by: query" in text
    assert "NOT in this corpus" in result.output or "unconfirmed.md" in result.output


def test_discovery_json_records_the_search_phase(wired) -> None:
    wired["use"]([SearchResult(url="https://maybe.example/post", snippet="Jane Smith")])
    seed_base(wired)

    run_with(wired)

    payload = json.loads((outputs(wired["out"], "janesmith") / "discovery.json").read_text())
    assert payload["search"]["searches"] >= 1
    assert payload["search"]["queries"], "the queries that ran are part of the record"
    assert payload["search"]["held"], "a held candidate must be recorded, not forgotten"


def test_a_run_that_read_no_candidate_page_says_so_rather_than_reporting_counts(
    wired,
) -> None:
    """A held count over unread candidates measures the fetcher, not the
    subject, and it is the number a reader would otherwise take at face value.

    The live checker refuses to print a census in this state; the run itself
    has to say it out loud for the same reason.
    """
    wired["use"]([SearchResult(url="https://unreachable.example/post", snippet="Jane Smith")])
    seed_base(wired)  # the candidate page is deliberately not seeded

    result = run_with(wired)
    assert result.exit_code == 0, result.output

    assert "no candidate page could be read" in result.output
    payload = json.loads((outputs(wired["out"], "janesmith") / "discovery.json").read_text())
    assert payload["search"]["unread"] is True
    assert payload["search"]["reads_attempted"] == 1
    assert payload["search"]["verified"] == 0


def test_no_search_runs_no_queries(wired) -> None:
    provider = wired["use"]([SearchResult(url="https://x.example/1")])
    seed_base(wired)

    result = run_with(wired, "--no-search")

    assert result.exit_code == 0, result.output
    assert provider.queries == []
    assert not (outputs(wired["out"], "janesmith") / "unconfirmed.md").exists()


def test_max_searches_caps_what_is_billed(wired) -> None:
    provider = wired["use"]([])
    seed_base(wired)

    run_with(wired, "--max-searches", "2")

    assert len(provider.queries) == 2


def test_a_missing_key_degrades_the_run_rather_than_ending_it(wired, monkeypatch) -> None:
    def boom(**_):
        from corpus.search.providers import SearchError

        raise SearchError("ANTHROPIC_API_KEY is not set, so search cannot run.")

    monkeypatch.setattr(cli_module, "get_search_provider", boom)
    seed_base(wired)

    result = run_with(wired)

    assert result.exit_code == 0, result.output
    assert "search did not run" in result.output
    corpus = json.loads((outputs(wired["out"], "janesmith") / "corpus.json").read_text())
    assert corpus, "the run still produced a corpus from phase 1"


# -- the accept-back path ---------------------------------------------------


def test_accepting_a_checked_entry_ingests_it_and_excludes_the_rest(wired) -> None:
    wired["use"]([SearchResult(url="https://maybe.example/post", snippet="Jane Smith")])
    seed_base(wired, {"https://maybe.example/post": THEIRS, "https://no.example/post": THEIRS})

    invoke(
        [
            "profile",
            "--key",
            "janesmith",
            "--name",
            "Jane Smith",
            "--employer",
            "Acme Corp",
            "--github",
            "jsmith",
        ]
    )
    unconfirmed = wired["out"] / "unconfirmed.md"
    unconfirmed.parent.mkdir(parents=True, exist_ok=True)
    unconfirmed.write_text(
        "- [x] https://maybe.example/post\n\n- [ ] https://no.example/post\n",
        encoding="utf-8",
    )

    result = invoke(
        [
            "run",
            "--target",
            "janesmith",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            "--no-search",
            "--accept-unconfirmed",
            str(unconfirmed),
        ]
    )
    assert result.exit_code == 0, result.output

    corpus = json.loads((outputs(wired["out"], "janesmith") / "corpus.json").read_text())
    accepted = [d for d in corpus if "maybe.example" in d["url"]]
    assert accepted, "a ticked entry was not ingested"
    assert accepted[0]["attribution"] == "corroborated"
    assert "user-confirmed" in accepted[0]["attribution_basis"]

    profiles = wired["profiles"].read_text()
    assert "https://no.example/post" in profiles, "a rejected URL must never resurface"
    assert "https://maybe.example/post" not in profiles


def test_an_unedited_file_is_not_acted_on_without_confirmation(wired) -> None:
    seed_base(wired)
    unconfirmed = wired["out"] / "unconfirmed.md"
    unconfirmed.parent.mkdir(parents=True, exist_ok=True)
    unconfirmed.write_text("- [ ] https://a.example/1\n- [ ] https://b.example/2\n", "utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "--name",
            "Jane Smith",
            "--github",
            "jsmith",
            "--out",
            str(wired["out"]),
            "--skip-synthesis",
            "--no-search",
            "--accept-unconfirmed",
            str(unconfirmed),
        ],
        input="n\ny\n",
        catch_exceptions=False,
    )

    assert "nothing is ticked" in result.output
    assert "Leaving the file alone" in result.output


# -- the provider seam, from the CLI ------------------------------------------


def test_an_unknown_search_provider_fails_before_anything_is_spent(wired, monkeypatch) -> None:
    """The estimate quotes the configured provider's rate, so a provider that
    does not exist must stop the run at flag-validation time — exit 2, like
    every other bad flag — not after a confirmation prompt quoted nonsense."""
    monkeypatch.setenv("SEARCH_PROVIDER", "nope")
    seed(wired, BASE_SEED)
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")

    result = run_with(wired)

    assert result.exit_code == 2
    assert "SEARCH_PROVIDER" in result.output
    assert "anthropic_search" in result.output


def test_no_search_ignores_a_broken_search_provider(wired, monkeypatch) -> None:
    """--no-search must mean it: a run that will never search cannot be
    stopped by the search provider's configuration."""
    monkeypatch.setenv("SEARCH_PROVIDER", "nope")
    seed(wired, BASE_SEED)
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")

    result = run_with(wired, "--no-search")

    assert result.exit_code == 0, result.output


def test_a_broken_reader_config_degrades_the_run_rather_than_ending_it(wired, monkeypatch) -> None:
    monkeypatch.setenv("READER_PROVIDER", "nope")
    seed(wired, BASE_SEED)
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")

    result = run_with(wired, "--reader-fallback")

    assert result.exit_code == 0, result.output
    assert "reader fallback not available" in result.output


def test_enabling_the_reader_fallback_says_so_out_loud(wired, monkeypatch) -> None:
    """Enabling it sends fallback URLs to a third party; a switch with that
    consequence must not flip silently."""
    monkeypatch.delenv("READER_PROVIDER", raising=False)
    seed(wired, BASE_SEED)
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")

    result = run_with(wired, "--reader-fallback")

    assert result.exit_code == 0, result.output
    assert "third-party reader service" in result.output


def test_a_registered_but_unpriced_stub_estimates_zero_and_does_not_crash(
    wired, monkeypatch
) -> None:
    """brave is in PROVIDERS (the name validates) but not in
    SEARCH_COST_BY_PROVIDER (nothing can bill against it). That gap crashed a
    run with a raw KeyError at the estimate — after the profile lookup was
    already paid for. The honest estimate for a phase that will refuse to
    construct is zero, said out loud; the phase itself then degrades to
    no-search exactly as it always did."""
    monkeypatch.setenv("SEARCH_PROVIDER", "brave")
    seed(wired, BASE_SEED)
    seed(wired, {"rss:https://janesmith.com/feed.xml": FEED}, source="rss")

    result = run_with(wired)

    assert result.exit_code == 0, result.output
    assert "no rate on file" in result.output
    assert "search (phase 2):        ~$0.000" in result.output
