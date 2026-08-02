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
    docs, _ = hydrate(client, load("tweets.json"), "testsubject", log=lambda _: None)
    synthesis = _synthesis([d.source_id for d in docs[:2]])
    synthesis.evolution = []
    report = render_report(
        handle="testsubject",
        synthesis=synthesis,
        docs=docs,
        signals=compute_signals(docs),
        budget_lines=[],
        run_meta={},
    )
    assert "No view changed inside this corpus. Not manufactured." in report


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
    for heading in ("## Themes", "## Hooks", "## Performance gap", "## Network",
                    "## Reading diet", "## Voice", "## Positions"):
        assert heading not in report


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
