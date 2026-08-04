"""The web source's date chain, and the unknown-date state it feeds.

The bug these tests pin: a live run stamped every page from the subject's own
blog with the run date, because the parser read one meta shape, found
nothing, and `parse_date` silently substituted the clock. A sixth of the
corpus landed in one fake chronological bucket and two "what moved" entries
came out inverted. The clock is not a publication date, and the tests below
hold the whole chain to that — including the load-bearing case where no date
exists at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from corpus.models import DATE_UNKNOWN, Document
from corpus.sources.base import make_document, parse_date
from corpus.sources.web import resolve_published_at
from corpus.x.signals import compute_signals

URL = "https://simonwillison.net/2026/Jul/16/kimi-k3"
PLAIN_URL = "https://blog.example.com/posts/kimi-k3"
JULY_16 = datetime(2026, 7, 16, tzinfo=timezone.utc)


def resolved(html: str, url: str = PLAIN_URL, text: str = "") -> datetime | None:
    return resolve_published_at(html, url, text)


# -- the five forms, each on its own --------------------------------------


def test_json_ld_date_published_resolves() -> None:
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "BlogPosting", "datePublished": "2026-07-16T14:19:30+00:00"}'
        "</script></head><body></body></html>"
    )
    when = resolved(html)
    assert when is not None and when.date() == JULY_16.date()


def test_meta_published_time_resolves() -> None:
    html = '<meta property="article:published_time" content="2026-07-16T14:19:30Z">'
    when = resolved(html)
    assert when is not None and when.date() == JULY_16.date()


def test_meta_og_updated_time_as_unix_seconds_resolves() -> None:
    """The exact shape on the live page: content="1784233170", unix seconds."""
    html = '<meta property="og:updated_time" content="1784233170">'
    when = resolved(html)
    assert when is not None and when.year == 2026 and when.month == 7


def test_time_tag_datetime_resolves() -> None:
    html = '<article><time datetime="2026-07-16">16th July</time></article>'
    when = resolved(html)
    assert when is not None and when.date() == JULY_16.date()


def test_month_name_in_url_path_resolves() -> None:
    when = resolved("<html><body></body></html>", url=URL)
    assert when is not None and when.date() == JULY_16.date()


def test_numeric_date_in_url_path_resolves() -> None:
    when = resolved("<html></html>", url="https://blog.example.com/2026/07/16/kimi-k3")
    assert when is not None and when.date() == JULY_16.date()


def test_visible_posted_text_resolves() -> None:
    when = resolved("<html></html>", text="Posted 16th July 2026 · 14 comments")
    assert when is not None and when.date() == JULY_16.date()


# -- the load-bearing case: no date is UNKNOWN, never the clock ------------


def test_a_page_with_no_date_is_unknown_and_never_today() -> None:
    html = "<html><head><title>Untitled</title></head><body><p>No dates here.</p></body></html>"
    assert resolved(html, url=PLAIN_URL, text="No dates here.") is None

    doc = make_document(
        source="web",
        source_id=PLAIN_URL,
        url=PLAIN_URL,
        author_handle="someone",
        published_at=None,
        title="Untitled",
        body="No dates here.",
        links=[],
        raw={},
    )
    assert doc.date_unknown is True
    assert doc.published_at == DATE_UNKNOWN
    assert doc.published_at.date() != datetime.now(tz=timezone.utc).date(), (
        "the clock leaked back into a document date"
    )


def test_parse_date_returns_none_not_the_clock() -> None:
    """The old fallback was datetime.now() on both of these inputs."""
    assert parse_date("") is None
    assert parse_date("not a date at all") is None
    assert parse_date(None) is None


def test_unknown_date_survives_corpus_json_round_trip() -> None:
    doc = make_document(
        source="web",
        source_id=PLAIN_URL,
        url=PLAIN_URL,
        author_handle="someone",
        published_at=None,
        title="t",
        body="b",
        links=[],
        raw={},
    )
    # The same round trip cli.py does: model_dump -> json -> model_validate.
    revived = Document.model_validate(json.loads(doc.model_dump_json()))
    assert revived.date_unknown is True
    assert revived.published_at == DATE_UNKNOWN


def test_an_old_corpus_json_loads_with_dates_known() -> None:
    """corpus.json files written before the flag existed default to known."""
    payload = {
        "source": "rss",
        "source_id": "1",
        "url": "https://a.example/1",
        "author_handle": "a",
        "published_at": "2024-03-12T10:00:00Z",
        "kind": "original",
        "body": "text",
    }
    assert Document.model_validate(payload).date_unknown is False


# -- an unknown date must not distort ordering or time signals -------------


def _doc(doc_id: str, when: datetime | None) -> Document:
    return make_document(
        source="web",
        source_id=doc_id,
        url=f"https://a.example/{doc_id}",
        author_handle="someone",
        published_at=when,
        title="",
        body=f"Body of {doc_id}, long enough to be a real document about rubrics.",
        links=[],
        raw={},
    )


def test_an_unknown_date_never_sorts_as_most_recent() -> None:
    docs = [
        _doc("old", datetime(2020, 1, 1, tzinfo=timezone.utc)),
        _doc("mystery", None),
        _doc("new", datetime(2026, 7, 16, tzinfo=timezone.utc)),
    ]
    # The exact sort cli.py applies to the corpus: newest first.
    docs.sort(key=lambda d: d.published_at, reverse=True)
    assert docs[0].source_id == "new"
    assert docs[-1].source_id == "mystery", "the unknown date must sink, not surface"


def test_signals_date_math_excludes_unknown_dates() -> None:
    docs = [
        _doc("a", datetime(2026, 6, 1, tzinfo=timezone.utc)),
        _doc("b", datetime(2026, 7, 1, tzinfo=timezone.utc)),
        _doc("mystery", None),
    ]
    signals = compute_signals(docs)
    assert signals["date_range"] == "2026-06-01 to 2026-07-01", (
        "an epoch placeholder stretched the date range"
    )
    assert signals["date_unknown_documents"] == 1
    # No decades-long fake hiatus from the epoch placeholder.
    assert all(h["days"] < 365 for h in signals["cadence"]["hiatuses"])
