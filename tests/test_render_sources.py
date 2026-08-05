"""Report rendering and the optional secondary sources."""

from __future__ import annotations

import json

from fake_anthropic import FakeAnthropic
from fake_provider import load

from corpus.models import Synthesis
from corpus.render import render_report
from corpus.sources.base import html_to_text
from corpus.sources.rss import RSSSource
from corpus.x.hydrate import hydrate
from corpus.x.signals import compute_signals


def _synthesis(ids: list[str]) -> Synthesis:
    marker = " ".join(f"[id: {i}]" for i in ids)
    return Synthesis.model_validate(
        json.loads(FakeAnthropic()._default_reduce({"messages": [{"c": marker}]}))
    )


def test_report_has_caveats_at_the_top_and_spend_at_the_bottom(client):
    docs, hyd = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    signals = compute_signals(docs)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=signals,
        budget_lines=["Total: $0.1234"],
        run_meta={
            "ingest": {
                "empty_windows": 2,
                "empty_window_ranges": [["2021-01-01", "2021-02-01"]],
                "stop_reason": "reached --max-posts (300)",
                "cursor_repeat_breaks": 1,
            },
            "hydration": hyd.as_dict(),
            # Pinned so the section headers below do not move with the tier.
            "analyzed_documents": 400,
            "corpus_tier": "rich",
        },
    )
    header, _, rest = report.partition("## Summary")
    assert "> **Coverage and caveats**" in header
    assert "may be provider gaps" in header
    assert "duplicate-cursor regression" in header
    assert report.index("## Spend") > report.index("## The generating model")
    assert "Total: $0.1234" in report


def test_every_claim_is_hyperlinked_to_its_source_post(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    ids = [d.source_id for d in docs[:2]]
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis(ids),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    for doc_id in ids:
        assert f"https://x.com/testsubject/status/{doc_id}" in report


def test_load_bearing_beliefs_come_before_derived_ones(client):
    """The report is a tree, not a list: roots first, what hangs off them under."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.core_model[1].belief = "A second-order view"
    synthesis.core_model[1].role = "derived"
    synthesis.core_model[1].evidence_ids = [docs[0].source_id]
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert report.index("### Process quality is measurable") < report.index(
        "### A second-order view"
    )
    # `generates` is what makes this a model rather than a list of opinions
    assert "→ hostility to interview puzzles" in report


def test_inference_is_rendered_apart_from_what_was_stated(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "**Stated.** Credentials are a lazy proxy for competence." in report
    assert "**Inferred** _(medium confidence)_" in report
    assert "_Chain:_" in report


def test_no_signal_axes_are_reported_not_hidden(client):
    """An axis the corpus cannot speak to is a finding. Silently omitting it
    would be indistinguishable from never having asked."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "**No signal:** defense intel natsec" in report


def test_evidence_links_are_capped_per_claim(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.core_model[0].evidence_ids = [d.source_id for d in docs[:6]]
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    belief_block = report.split("### Process quality is measurable")[1].split("###")[0]
    assert belief_block.count("](https://x.com/") <= 3


def test_filter_drop_count_lands_in_the_coverage_block(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"filter": {"dropped": 4, "by_reason": {"acknowledgement": 4}}},
    )
    assert "4 low-signal document(s) filtered" in report
    assert "nothing was filtered by subject" in report
    assert "--no-filter" in report


def test_report_survives_a_failed_synthesis(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=None,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=["Total: $0.02"],
        run_meta={"synthesis_error": "validation failed twice"},
    )
    assert "## No synthesis" in report
    assert "validation failed twice" in report
    assert "Total: $0.02" in report


def test_empty_evolution_says_so_rather_than_inventing_one(client):
    """On a corpus big enough to have found one, empty means empty."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.evolution = []
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"analyzed_documents": 400, "corpus_tier": "rich"},
    )
    assert "No view changed inside this corpus. Not manufactured." in report


def test_empty_evolution_on_a_thin_corpus_says_it_was_not_assessed(client):
    """ "Nothing found" and "we did not look" are different claims, and only one
    of them is true here."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.evolution = []
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"analyzed_documents": 12, "corpus_tier": "thin"},
    )
    assert "Not assessed" in report
    assert "Not manufactured" not in report


def test_cut_sections_are_gone(client):
    """performance_gap, hooks, themes, and the network/reading-diet tables were
    about reach and outreach, not cognition."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    for heading in (
        "## Themes",
        "## Hooks",
        "## Performance gap",
        "## Network",
        "## Reading diet",
        "## Voice",
        "## Positions",
    ):
        assert heading not in report


# -- attribution ------------------------------------------------------------


def test_an_all_anchor_corpus_gets_no_attribution_line(client):
    """The unmarked case must be the certain one. A line reading "100% certain"
    on every report trains the reader to skip the one where it matters."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "- Attribution:" not in report


def test_a_mixed_corpus_shows_the_mix_and_names_the_weak_half(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    docs[1].attribution = "linked"
    docs[2].attribution = "corroborated"
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "- Attribution:" in report
    assert "1 linked" in report
    assert "1 corroborated" in report
    assert "matched rather than supplied" in report


def test_a_finding_resting_only_on_corroborated_evidence_is_flagged(client):
    """Corroborated is good enough to ingest and not good enough to let a claim
    stand on it silently."""
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([docs[0].source_id])
    synthesis.core_model[0].evidence_ids = [docs[0].source_id]
    docs[0].attribution = "corroborated"
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "corroborated sources only" in report


def test_one_anchor_among_the_evidence_removes_the_flag(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.core_model[0].evidence_ids = [docs[0].source_id, docs[1].source_id]
    docs[0].attribution = "corroborated"  # docs[1] stays an anchor
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    belief_block = report.split("### Process quality is measurable")[1].split("###")[0]
    assert "corroborated sources only" not in belief_block


def test_held_candidates_are_named_in_the_coverage_block(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="testsubject",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={"discovery": {"held": [{"url": "https://someone-else.example"}]}},
    )
    assert "matched the name and nothing else" in report
    assert "discovery.json" in report


def test_the_title_names_the_person_when_there_is_no_handle(client):
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    report = render_report(
        handle="janesmith",
        subject="Jane Smith",
        synthesis=_synthesis([d.source_id for d in docs[:2]]),
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert report.startswith("# Jane Smith — how they think")


# -- secondary sources ------------------------------------------------------


def test_html_to_text_drops_script_and_nav():
    html = """
    <html><head><style>.a{color:red}</style></head>
    <body><nav>menu</nav><article><h1>Title</h1><p>First para with
    a <a href="https://example.com/x">link</a>.</p><script>alert(1)</script>
    <p>Second para.</p></article></body></html>
    """
    text, links = html_to_text(html)
    assert "menu" not in text
    assert "alert" not in text
    assert "color:red" not in text
    assert "First para" in text and "Second para" in text
    assert links == ["https://example.com/x"]


def test_rss_source_parses_rss_2_0(cache, tmp_path):
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Blog</title>
      <item>
        <title>On rubrics</title>
        <link>https://blog.example.com/rubrics</link>
        <pubDate>Tue, 12 Mar 2024 10:00:00 +0000</pubDate>
        <description>&lt;p&gt;Write the rubric first.&lt;/p&gt;</description>
        <guid>https://blog.example.com/rubrics</guid>
      </item>
    </channel></rss>"""
    cache.put("rss", "rss:https://blog.example.com/feed", xml)
    docs = RSSSource().fetch(
        "https://blog.example.com/feed",
        author_handle="testsubject",
        cache=cache,
        log=lambda _: None,
    )
    assert len(docs) == 1
    assert docs[0].source == "rss"
    assert "On rubrics" in docs[0].body
    assert "Write the rubric first." in docs[0].body
    assert docs[0].published_at.year == 2024


def test_rss_source_parses_atom(cache):
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Evals</title>
        <link href="https://blog.example.com/evals"/>
        <published>2025-01-04T09:00:00Z</published>
        <content>Ground truth is the hard part.</content>
        <id>tag:blog,2025:evals</id>
      </entry>
    </feed>"""
    cache.put("rss", "rss:https://blog.example.com/atom", xml)
    docs = RSSSource().fetch(
        "https://blog.example.com/atom",
        author_handle="testsubject",
        cache=cache,
        log=lambda _: None,
    )
    assert len(docs) == 1
    assert docs[0].published_at.year == 2025
    assert "Ground truth" in docs[0].body


def test_secondary_documents_merge_into_the_same_corpus(client, cache):
    """A secondary source must need no changes in signals or synthesis."""
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Post</title><link>https://b.example/p</link>
      <pubDate>Tue, 12 Mar 2024 10:00:00 +0000</pubDate>
      <description>Body about rubrics and evaluation.</description></item>
    </channel></rss>"""
    cache.put("rss", "rss:https://b.example/feed", xml)
    x_docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    rss_docs = RSSSource().fetch(
        "https://b.example/feed", author_handle="testsubject", cache=cache, log=lambda _: None
    )
    signals = compute_signals(x_docs + rss_docs)
    assert signals["total_documents"] == len(x_docs) + 1
    assert signals["kind_mix"]["counts"]["original"] >= 1


def test_atom_entry_dates_are_the_entrys_own(cache):
    """Each entry's date comes from that entry, never from a sibling."""
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <updated>2020-04-30T00:00:00+00:00</updated>
      <entry><title>A</title><link href="https://t.example/a"/><id>a</id>
        <published>2024-03-01T10:00:00Z</published><content>First body.</content></entry>
      <entry><title>B</title><link href="https://t.example/b"/><id>b</id>
        <updated>2025-07-16T10:00:00Z</updated><content>Second body.</content></entry>
    </feed>"""
    cache.put("rss", "rss:https://t.example/feed", xml)
    docs = RSSSource().fetch(
        "https://t.example/feed", author_handle="s", cache=cache, log=lambda _: None
    )
    dates = {d.source_id: d.published_at.date().isoformat() for d in docs}
    assert dates == {"a": "2024-03-01", "b": "2025-07-16"}
    assert not any(d.date_unknown for d in docs)


def test_an_undated_atom_entry_is_unknown_and_never_inherits_the_feed_date(cache):
    """The TIL-feed failure: a feed-level <updated> of 2020-04-30 must not
    stamp every dateless entry. An entry with no date of its own has an
    unknown date, stated as such."""
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>TIL</title>
      <updated>2020-04-30T00:00:00+00:00</updated>
      <entry><title>No date</title><link href="https://til.example/x"/><id>x</id>
        <content>A dateless entry.</content></entry>
    </feed>"""
    cache.put("rss", "rss:https://til.example/feed", xml)
    docs = RSSSource().fetch(
        "https://til.example/feed", author_handle="s", cache=cache, log=lambda _: None
    )
    assert len(docs) == 1
    assert docs[0].date_unknown is True
    assert docs[0].published_at.year != 2020, "the feed's own date leaked into the entry"


def test_an_undated_rss_item_does_not_inherit_the_channel_pubdate(cache):
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Blog</title>
      <pubDate>Thu, 30 Apr 2020 00:00:00 +0000</pubDate>
      <lastBuildDate>Thu, 30 Apr 2020 00:00:00 +0000</lastBuildDate>
      <item><title>No date</title><link>https://b.example/x</link>
        <description>A dateless item.</description><guid>x</guid></item>
    </channel></rss>"""
    cache.put("rss", "rss:https://b.example/feed", xml)
    docs = RSSSource().fetch(
        "https://b.example/feed", author_handle="s", cache=cache, log=lambda _: None
    )
    assert len(docs) == 1
    assert docs[0].date_unknown is True
    assert docs[0].published_at.year != 2020
