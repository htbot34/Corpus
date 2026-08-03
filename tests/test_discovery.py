"""Phase 1 discovery: following links out from the anchors.

Every test here runs against a pre-seeded cache, so no test can reach the
network — and to prove it rather than assume it, the fixture replaces
`http_client` with something that fails the test if it is ever constructed.

The interesting assertions are all about the *scoring*, not the crawling. A
crawler that finds more is easy; the hard part is not attributing a stranger's
blog to the subject, and that is what most of this file pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus import discovery as discovery_module
from corpus.cache import Cache
from corpus.discovery import (
    Candidate,
    discover_from_anchors,
    feed_links,
    normalize_url,
    plan_from_anchors,
)
from corpus.identity import IdentityCard

FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>On rubrics</title><link>https://janesmith.com/rubrics</link>
<pubDate>Tue, 12 Mar 2024 10:00:00 +0000</pubDate>
<description>Write the rubric first.</description></item>
</channel></rss>"""


@pytest.fixture()
def cache(tmp_path: Path) -> Cache:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Discovery is offline in tests, and the test fails loudly if it is not."""

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery opened an HTTP client during a test")

    monkeypatch.setattr(discovery_module, "http_client", forbidden)


def seed(cache: Cache, url: str, body: str) -> None:
    cache.put("discovery", f"get:{url}", body)


def card(**kwargs) -> IdentityCard:
    base = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "anchors": {"github": "jsmith"},
    }
    base.update(kwargs)
    return IdentityCard(**base)


def urls(candidates: list[Candidate]) -> set[str]:
    return {c.url for c in candidates}


# -- URL handling -----------------------------------------------------------


def test_one_page_has_one_spelling() -> None:
    same = {
        normalize_url("https://JaneSmith.com/blog/"),
        normalize_url("https://www.janesmith.com/blog"),
        normalize_url("https://janesmith.com/blog#intro"),
        normalize_url("https://janesmith.com/blog?utm_source=twitter"),
    }
    assert same == {"https://janesmith.com/blog"}


def test_a_non_http_url_is_not_a_url() -> None:
    assert normalize_url("mailto:jane@example.com") == ""
    assert normalize_url("javascript:void(0)") == ""


def test_feed_links_are_read_from_the_head_and_resolved() -> None:
    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" href="/index.xml">
      <link rel="alternate" type="application/atom+xml" href="https://other.example/atom">
      <link rel="alternate" type="text/html" href="/amp">
    </head><body></body></html>"""
    found = feed_links(html, "https://janesmith.com/")
    assert found == ["https://janesmith.com/index.xml", "https://other.example/atom"]


# -- the plan ---------------------------------------------------------------


def test_the_plan_names_what_would_be_fetched_without_fetching_it() -> None:
    plan = plan_from_anchors(
        card(anchors={"x": "janesmith", "github": "jsmith", "site": "https://janesmith.com"})
    )
    assert any("api.github.com/users/jsmith" in line for line in plan)
    assert any("janesmith.com" in line for line in plan)
    assert any("X bio" in line for line in plan)


# -- anchors are certain ----------------------------------------------------


def test_the_anchors_themselves_come_back_as_anchor_attribution(cache: Cache) -> None:
    result = discover_from_anchors(
        card(anchors={"substack": "janesmith.substack.com"}), cache, log=lambda _: None
    )
    seeds = [c for c in result.candidates if c.attribution == "anchor"]
    assert [c.url for c in seeds] == ["https://janesmith.substack.com"]
    assert seeds[0].kind == "substack"
    assert seeds[0].confidence == 1.0


def test_an_x_only_target_discovers_nothing_and_costs_nothing(cache: Cache) -> None:
    """The common case today: no anchors to follow, no fetches, no errors."""
    result = discover_from_anchors(
        card(anchors={"x": "janesmith"}), cache, x_profile={"url": None}, log=lambda _: None
    )
    assert result.candidates == []
    assert result.fetches == 0


# -- the declared surfaces --------------------------------------------------


def test_a_github_bio_link_is_linked_even_on_an_unrelated_host(cache: Cache) -> None:
    """The highest-value path in the whole layer: people link their own things,
    and a `blog` field is a self-declaration rather than a guess."""
    seed(
        cache,
        "https://api.github.com/users/jsmith",
        json.dumps({"blog": "https://thinking-out-loud.example", "bio": "VP Eng at Acme"}),
    )
    result = discover_from_anchors(card(), cache, log=lambda _: None)
    found = [c for c in result.candidates if "thinking-out-loud" in c.url]
    assert found, "a declared blog was not followed"
    assert found[0].attribution == "linked"
    assert "bio" in found[0].basis


def test_an_x_bio_url_is_followed_without_a_single_extra_fetch(cache: Cache) -> None:
    """The profile was already paid for by the run; the bio comes free with it."""
    profile = {
        "userName": "janesmith",
        "url": "https://t.co/abc",
        "description": "Writing at https://janesmith.substack.com",
    }
    result = discover_from_anchors(
        card(anchors={"x": "janesmith"}), cache, x_profile=profile, log=lambda _: None
    )
    assert result.fetches == 0, "reading a bio the run already holds cost a request"
    assert urls(result.candidates) == {"https://janesmith.substack.com"}
    assert result.candidates[0].attribution == "linked"


def test_a_blog_found_in_a_bio_is_then_resolved_to_its_feed(cache: Cache) -> None:
    """The chain the whole phase exists for: bio -> blog -> feed -> 50 posts,
    at zero cost and near-certain attribution."""
    seed(
        cache,
        "https://api.github.com/users/jsmith",
        json.dumps({"blog": "https://thinking.example"}),
    )
    seed(
        cache,
        "https://thinking.example",
        '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        "</head><body>hi</body></html>",
    )
    result = discover_from_anchors(card(), cache, log=lambda _: None)
    feed = next(c for c in result.candidates if c.url.endswith("feed.xml"))
    assert feed.kind == "rss"
    assert feed.attribution == "linked"
    assert "https://thinking.example" not in urls(result.candidates), (
        "the homepage should give way to the feed it declared"
    )


def test_a_pinned_post_is_read_only_when_a_lookup_is_supplied(cache: Cache) -> None:
    profile = {"userName": "janesmith", "pinnedTweetIds": ["99"]}
    calls: list[list[str]] = []

    def lookup(ids: list[str]) -> dict[str, dict[str, object]]:
        calls.append(ids)
        return {"99": {"text": "everything I think is at https://janesmith.com/essays"}}

    without = discover_from_anchors(
        card(anchors={"x": "janesmith"}), cache, x_profile=profile, log=lambda _: None
    )
    assert calls == [], "the metered lookup ran without being asked for"
    assert without.candidates == []

    with_lookup = discover_from_anchors(
        card(anchors={"x": "janesmith"}),
        cache,
        x_profile=profile,
        x_lookup=lookup,
        log=lambda _: None,
    )
    assert calls == [["99"]]
    assert "https://janesmith.com/essays" in urls(with_lookup.candidates)


def test_a_failing_pinned_lookup_degrades_rather_than_raising(cache: Cache) -> None:
    def lookup(ids: list[str]) -> dict[str, dict[str, object]]:
        raise RuntimeError("budget exhausted")

    result = discover_from_anchors(
        card(anchors={"x": "janesmith"}),
        cache,
        x_profile={"userName": "janesmith", "pinnedTweetIds": ["99"]},
        x_lookup=lookup,
        log=lambda _: None,
    )
    assert any("pinned post lookup failed" in e for e in result.errors)


# -- the page surfaces, where the scoring earns its keep --------------------


def test_a_readme_link_to_a_stranger_is_held_not_ingested(cache: Cache) -> None:
    """The failure this layer exists to prevent. A profile README is prose full
    of other people's projects; treating it as a self-declaration would
    attribute half of someone's reading list to them."""
    seed(cache, "https://api.github.com/users/jsmith", json.dumps({"blog": ""}))
    seed(
        cache,
        "https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md",
        "Reading: https://someone-else.example/great-post and my own https://jsmith.dev",
    )
    result = discover_from_anchors(card(), cache, log=lambda _: None)

    assert "https://jsmith.dev" in urls(result.candidates), "their own host was not recognised"
    assert "https://someone-else.example/great-post" in urls(result.held)
    assert "https://someone-else.example/great-post" not in urls(result.candidates)

    stranger = next(c for c in result.held if "someone-else" in c.url)
    assert stranger.attribution == "name_match"
    assert stranger.missing, "a held candidate must say what did not match"


def test_a_link_on_an_anchored_host_is_linked_whatever_it_says(cache: Cache) -> None:
    seed(
        cache,
        "https://janesmith.com",
        '<html><body><a href="https://janesmith.com/essays/one">Essay</a></body></html>',
    )
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    essay = next(c for c in result.candidates if c.url.endswith("/essays/one"))
    assert essay.attribution == "linked"
    assert "anchors" in " ".join(essay.signals)


def test_an_excluded_url_is_never_a_candidate(cache: Cache) -> None:
    """A known false positive stays a false positive, however it is reached."""
    seed(
        cache,
        "https://api.github.com/users/jsmith",
        json.dumps({"blog": "https://linkedin.com/in/jane-smith-attorney"}),
    )
    subject = card(exclude=["https://linkedin.com/in/jane-smith-attorney"])
    result = discover_from_anchors(subject, cache, log=lambda _: None)
    assert not any("attorney" in c.url for c in result.candidates + result.held)
    assert any("excluded by the card" in note for note in result.notes)


def test_platforms_we_will_not_read_are_refused_with_the_reason(cache: Cache) -> None:
    seed(
        cache,
        "https://api.github.com/users/jsmith",
        json.dumps({"bio": "https://www.linkedin.com/in/jsmith"}),
    )
    result = discover_from_anchors(card(), cache, log=lambda _: None)
    assert not any("linkedin" in c.url for c in result.candidates + result.held)
    assert any("LinkedIn" in note for note in result.notes)


def test_a_profile_link_back_to_an_anchor_corroborates_without_being_a_source(
    cache: Cache,
) -> None:
    seed(
        cache,
        "https://janesmith.com",
        '<html><body><a href="https://github.com/jsmith">code</a></body></html>',
    )
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com", "github": "jsmith"}),
        cache,
        log=lambda _: None,
    )
    assert not any(c.url == "https://github.com/jsmith" and c.kind != "github" for c in result.held)
    assert any("corroborates the card" in note for note in result.notes)


def test_badge_and_licence_hosts_are_not_findings(cache: Cache) -> None:
    seed(cache, "https://api.github.com/users/jsmith", json.dumps({"blog": ""}))
    seed(
        cache,
        "https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md",
        "![build](https://img.shields.io/badge/x.svg) https://opensource.org/licenses/MIT",
    )
    result = discover_from_anchors(card(), cache, log=lambda _: None)
    assert result.held == []
    assert not any("shields.io" in c.url for c in result.candidates)


# -- probing ----------------------------------------------------------------


def test_a_declared_feed_is_preferred_to_probing(cache: Cache) -> None:
    seed(
        cache,
        "https://janesmith.com",
        '<html><head><link rel="alternate" type="application/rss+xml" href="/index.xml">'
        "</head><body>hi</body></html>",
    )
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    assert "https://janesmith.com/index.xml" in urls(result.candidates)
    # Nothing was probed: every probe path would have been a cache miss, and a
    # cache miss in this test raises.
    assert result.errors == []


def test_a_site_with_no_declared_feed_is_probed(cache: Cache) -> None:
    seed(cache, "https://janesmith.com", "<html><body>Hello.</body></html>")
    seed(cache, "https://janesmith.com/feed", FEED_XML)
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    feed = next(c for c in result.candidates if c.url.endswith("/feed"))
    assert feed.kind == "rss"
    assert feed.attribution == "linked"


def test_a_probe_that_returns_html_is_not_mistaken_for_a_feed(cache: Cache) -> None:
    seed(cache, "https://janesmith.com", "<html><body>Hello.</body></html>")
    seed(cache, "https://janesmith.com/feed", "<html><body>404, sorry</body></html>")
    seed(cache, "https://janesmith.com/rss.xml", "<html><body>404, sorry</body></html>")
    seed(cache, "https://janesmith.com/atom.xml", "<html><body>404, sorry</body></html>")
    seed(
        cache,
        "https://janesmith.com/blog",
        "<html><body><p>" + ("Long-form prose. " * 40) + "</p></body></html>",
    )
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    assert not any(c.kind == "rss" for c in result.candidates)
    assert "https://janesmith.com/blog" in urls(result.candidates)


def test_the_homepage_gives_way_to_the_feed_it_pointed_at(cache: Cache) -> None:
    """A homepage is a nav bar with a photograph once the feed is in hand."""
    seed(
        cache,
        "https://janesmith.com",
        '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        "</head><body>hi</body></html>",
    )
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    assert "https://janesmith.com" not in urls(result.candidates)
    assert "https://janesmith.com/feed.xml" in urls(result.candidates)


def test_the_fetch_cap_stops_the_walk_and_says_so(cache: Cache) -> None:
    seed(cache, "https://api.github.com/users/jsmith", json.dumps({"blog": ""}))
    result = discover_from_anchors(
        card(anchors={"github": "jsmith", "site": "https://janesmith.com"}),
        cache,
        max_fetches=0,
        log=lambda _: None,
    )
    assert any("fetch cap" in e for e in result.errors)
    assert any("--max-fetches" in e for e in result.errors)


def test_a_dead_host_degrades_the_corpus_rather_than_the_run(cache: Cache) -> None:
    """Discovery runs before anything is paid for and must never be the reason
    a run dies."""
    result = discover_from_anchors(
        card(anchors={"site": "https://janesmith.com"}), cache, log=lambda _: None
    )
    # Nothing cached and the client is forbidden, so every fetch failed.
    assert result.errors
    assert "https://janesmith.com" in urls(result.candidates)  # the anchor still stands


def test_the_same_find_from_two_surfaces_keeps_the_stronger_claim(cache: Cache) -> None:
    seed(
        cache,
        "https://api.github.com/users/jsmith",
        json.dumps({"blog": "https://janesmith.com"}),
    )
    seed(
        cache,
        "https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md",
        "see https://janesmith.com",
    )
    seed(cache, "https://janesmith.com", "<html><body>hi</body></html>")
    result = discover_from_anchors(
        card(anchors={"github": "jsmith", "site": "https://janesmith.com"}),
        cache,
        log=lambda _: None,
    )
    matches = [c for c in result.candidates if c.url == "https://janesmith.com"]
    assert len(matches) == 1, "the same page was counted twice"
    assert matches[0].attribution == "anchor"


def test_a_second_run_over_the_same_target_costs_no_fetches(cache: Cache) -> None:
    seed(cache, "https://api.github.com/users/jsmith", json.dumps({"blog": "https://jsmith.dev"}))
    seed(cache, "https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md", "hello")
    seed(
        cache,
        "https://jsmith.dev",
        '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        "</head><body>hi</body></html>",
    )
    first = discover_from_anchors(card(), cache, log=lambda _: None)
    second = discover_from_anchors(card(), cache, log=lambda _: None)
    assert first.fetches == second.fetches == 0
    assert second.cached_fetches == first.cached_fetches >= 3
    assert urls(first.candidates) == urls(second.candidates)


def test_the_result_serializes_everything_a_reader_would_need(cache: Cache) -> None:
    seed(cache, "https://api.github.com/users/jsmith", json.dumps({"blog": "https://jsmith.dev"}))
    payload = discover_from_anchors(card(), cache, log=lambda _: None).as_dict()
    assert payload["searches"] == 0, "phase 2 has not run; that must be explicit"
    assert payload["by_attribution"]["linked"] >= 1
    entry = payload["candidates"][0]
    assert {"url", "kind", "attribution", "attribution_basis", "signals"} <= set(entry)
    json.dumps(payload)  # it lands in discovery.json, so it has to serialize
