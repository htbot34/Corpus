"""Derived signals, computed in Python — never by the model.

Models are bad at arithmetic and confident about it. Every number the synthesis
is allowed to state comes from here, gets written to signals.json, and is
injected into the reduce prompt as ground truth.

Pure functions over a list of Documents. Zero API calls, fully unit-testable.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from urllib.parse import urlparse

from ..models import Document

HIATUS_DAYS = 14
TOP_NETWORK = 25
TOP_DOMAINS = 25
TOP_TERMS_PER_BUCKET = 15
BUCKET_MONTHS = 6

_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")
_SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s|$)")
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@\w+")

# Deliberately small: an English stoplist plus the words every X corpus is
# saturated with. Anything longer starts deleting signal.
STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "this",
    "with",
    "you",
    "not",
    "but",
    "are",
    "have",
    "has",
    "was",
    "were",
    "they",
    "them",
    "their",
    "there",
    "then",
    "than",
    "from",
    "what",
    "when",
    "who",
    "how",
    "why",
    "all",
    "any",
    "can",
    "will",
    "would",
    "should",
    "could",
    "just",
    "like",
    "get",
    "got",
    "one",
    "out",
    "about",
    "more",
    "most",
    "some",
    "such",
    "only",
    "very",
    "even",
    "also",
    "into",
    "over",
    "your",
    "our",
    "its",
    "it's",
    "i'm",
    "don't",
    "doesn't",
    "didn't",
    "you're",
    "we're",
    "they're",
    "isn't",
    "aren't",
    "much",
    "many",
    "way",
    "make",
    "made",
    "makes",
    "know",
    "think",
    "really",
    "people",
    "thing",
    "things",
    "because",
    "which",
    "been",
    "being",
    "here",
    "does",
    "did",
    "yes",
    "yeah",
    "well",
    "still",
    "now",
    "new",
    "good",
    "great",
    "want",
    "need",
    "see",
    "look",
    "going",
    "gonna",
    "actually",
    "http",
    "https",
    "com",
    "rt",
}

SYNTHESIZABLE_KINDS = ("original", "thread", "reply", "quote")


def _tokens(text: str) -> list[str]:
    text = _URL.sub(" ", text)
    text = _MENTION.sub(" ", text)
    return [w.lower() for w in _WORD.findall(text) if w.lower() not in STOPWORDS and len(w) > 2]


def _domain(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    return round(statistics.fmean(vals), 2) if vals else 0.0


def _median(values: Iterable[float]) -> float:
    vals = list(values)
    return round(statistics.median(vals), 2) if vals else 0.0


# --------------------------------------------------------------------------


def cadence(docs: list[Document]) -> dict[str, Any]:
    """Posts per month over the full range, plus bursts and 14+ day hiatuses."""
    if not docs:
        return {
            "posts_per_month": {},
            "mean_posts_per_month": 0.0,
            "median_posts_per_month": 0.0,
            "bursts": [],
            "hiatuses": [],
            "active_months": 0,
            "silent_months": 0,
        }

    by_month: Counter[str] = Counter()
    for doc in docs:
        by_month[doc.published_at.strftime("%Y-%m")] += 1

    ordered = sorted(d.published_at for d in docs)
    start, end = ordered[0], ordered[-1]

    # Fill silent months so the series shows absence, not just presence.
    months: list[str] = []
    cursor = datetime(start.year, start.month, 1, tzinfo=start.tzinfo)
    end_marker = datetime(end.year, end.month, 1, tzinfo=end.tzinfo)
    while cursor <= end_marker:
        months.append(cursor.strftime("%Y-%m"))
        cursor = datetime(
            cursor.year + (cursor.month // 12),
            (cursor.month % 12) + 1,
            1,
            tzinfo=cursor.tzinfo,
        )
    series = {m: by_month.get(m, 0) for m in months}

    counts = list(series.values())
    mean = statistics.fmean(counts) if counts else 0.0
    stdev = statistics.pstdev(counts) if len(counts) > 1 else 0.0
    burst_floor = mean + 1.5 * stdev
    bursts = [
        {"month": m, "posts": c}
        for m, c in series.items()
        if c > burst_floor and c > mean and stdev > 0
    ]

    hiatuses = []
    for earlier, later in pairwise(ordered):
        gap = later - earlier
        if gap >= timedelta(days=HIATUS_DAYS):
            hiatuses.append(
                {
                    "from": earlier.strftime("%Y-%m-%d"),
                    "to": later.strftime("%Y-%m-%d"),
                    "days": gap.days,
                }
            )

    return {
        "posts_per_month": series,
        "mean_posts_per_month": round(mean, 2),
        "median_posts_per_month": _median(counts),
        "bursts": sorted(bursts, key=lambda b: -b["posts"]),
        "hiatuses": sorted(hiatuses, key=lambda h: -h["days"]),
        "active_months": sum(1 for c in counts if c > 0),
        "silent_months": sum(1 for c in counts if c == 0),
    }


def kind_mix(docs: list[Document]) -> dict[str, Any]:
    counts = Counter(d.kind for d in docs)
    total = len(docs) or 1
    return {
        "counts": dict(counts),
        "shares": {k: round(v / total, 4) for k, v in counts.items()},
        "total": len(docs),
    }


def conversation_graph(docs: list[Document]) -> list[dict[str, Any]]:
    """Handles they reply to, ranked by count.

    This is their real intellectual network, and it beats a follower list:
    following is cheap, replying repeatedly is not.
    """
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    author = docs[0].author_handle.lower() if docs else ""
    for doc in docs:
        if doc.kind not in ("reply", "thread", "quote"):
            continue
        handle = (doc.context_author or "").lstrip("@")
        if not handle or handle.lower() == author:
            continue
        counts[handle] += 1
        if len(samples[handle]) < 3:
            samples[handle].append(doc.source_id)
    return [
        {"handle": h, "exchange_count": c, "example_ids": samples[h]}
        for h, c in counts.most_common(TOP_NETWORK)
    ]


def outbound_domains(docs: list[Document]) -> list[dict[str, Any]]:
    """What someone reads is what they think about."""
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        for link in doc.outbound_links:
            dom = _domain(link)
            if not dom:
                continue
            counts[dom] += 1
            if len(samples[dom]) < 3:
                samples[dom].append(doc.source_id)
    return [
        {"domain": d, "share_count": c, "example_ids": samples[d]}
        for d, c in counts.most_common(TOP_DOMAINS)
    ]


def engagement_baselines(docs: list[Document]) -> dict[str, Any]:
    """Median engagement by kind, so the synthesis can flag genuine outliers
    instead of calling every popular post notable."""
    by_kind: dict[str, list[Document]] = defaultdict(list)
    for doc in docs:
        by_kind[doc.kind].append(doc)

    out: dict[str, Any] = {}
    for kind, group in by_kind.items():
        out[kind] = {
            "n": len(group),
            "median_likes": _median(d.engagement.get("likes", 0) for d in group),
            "median_replies": _median(d.engagement.get("replies", 0) for d in group),
            "median_reposts": _median(d.engagement.get("reposts", 0) for d in group),
            "median_views": _median(d.engagement.get("views", 0) for d in group),
            "p90_likes": (
                round(
                    sorted(d.engagement.get("likes", 0) for d in group)[
                        max(0, int(len(group) * 0.9) - 1)
                    ],
                    2,
                )
                if group
                else 0.0
            ),
        }

    outliers = []
    for doc in docs:
        base = out.get(doc.kind, {}).get("median_likes", 0)
        likes = doc.engagement.get("likes", 0)
        if base and likes >= base * 5 and likes > 10:
            outliers.append(
                {
                    "id": doc.source_id,
                    "kind": doc.kind,
                    "likes": likes,
                    "multiple_of_median": round(likes / base, 1),
                    "url": doc.url,
                }
            )
    outliers.sort(key=lambda o: -o["multiple_of_median"])
    return {"by_kind": out, "outliers": outliers[:20]}


def register_split(docs: list[Document]) -> dict[str, Any]:
    """Mean word count and mean sentence length, by kind.

    People perform in top-level posts and argue in replies; the numbers show it.
    """
    out: dict[str, Any] = {}
    by_kind: dict[str, list[Document]] = defaultdict(list)
    for doc in docs:
        by_kind[doc.kind].append(doc)

    for kind, group in by_kind.items():
        word_counts = [d.word_count for d in group]
        sentence_lengths: list[float] = []
        for doc in group:
            sentences = [s for s in _SENTENCE_SPLIT.split(doc.body) if s.strip()]
            if sentences:
                sentence_lengths.append(statistics.fmean(len(s.split()) for s in sentences))
        out[kind] = {
            "n": len(group),
            "mean_word_count": _mean(word_counts),
            "mean_sentence_length": _mean(sentence_lengths),
        }
    return out


def vocabulary_drift(docs: list[Document]) -> list[dict[str, Any]]:
    """Top distinctive terms per 6-month bucket, TF-IDF against their own corpus.

    Surfaces topic movement with zero model calls. IDF is computed across the
    person's own buckets, so a term that is constant for them scores near zero
    and only genuinely time-localized vocabulary rises.
    """
    if not docs:
        return []

    buckets: dict[str, list[str]] = defaultdict(list)
    for doc in docs:
        if doc.kind == "media_only":
            continue
        half = 1 if doc.published_at.month <= BUCKET_MONTHS else 2
        label = f"{doc.published_at.year}-H{half}"
        buckets[label].extend(_tokens(doc.body))

    buckets = {k: v for k, v in buckets.items() if len(v) >= 30}
    if len(buckets) < 2:
        return []

    n_buckets = len(buckets)
    doc_freq: Counter[str] = Counter()
    for tokens in buckets.values():
        for term in set(tokens):
            doc_freq[term] += 1

    results = []
    for label in sorted(buckets):
        tokens = buckets[label]
        total = len(tokens)
        tf = Counter(tokens)
        scored = []
        for term, count in tf.items():
            if count < 3:
                continue
            idf = math.log(n_buckets / (1 + doc_freq[term])) + 1.0
            scored.append((term, (count / total) * idf, count))
        scored.sort(key=lambda x: -x[1])
        results.append(
            {
                "bucket": label,
                "token_count": total,
                "terms": [
                    {"term": t, "score": round(s, 6), "count": c}
                    for t, s, c in scored[:TOP_TERMS_PER_BUCKET]
                ],
            }
        )
    return results


def compute_signals(docs: list[Document], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything above, in one JSON-serializable dict."""
    synth = [d for d in docs if d.kind in SYNTHESIZABLE_KINDS]
    dates = sorted(d.published_at for d in docs)
    date_range = (
        f"{dates[0].strftime('%Y-%m-%d')} to {dates[-1].strftime('%Y-%m-%d')}" if dates else "empty"
    )
    signals: dict[str, Any] = {
        "author_handle": docs[0].author_handle if docs else "",
        "date_range": date_range,
        "total_documents": len(docs),
        "synthesizable_documents": len(synth),
        # Cadence counts every document, media-only included: silence and
        # low-effort posting are both real signals about tempo.
        "cadence": cadence(docs),
        "kind_mix": kind_mix(docs),
        "conversation_graph": conversation_graph(docs),
        "outbound_domains": outbound_domains(docs),
        "engagement_baselines": engagement_baselines(docs),
        "register_split": register_split(docs),
        "vocabulary_drift": vocabulary_drift(synth),
    }
    if extra:
        signals.update(extra)
    return signals
