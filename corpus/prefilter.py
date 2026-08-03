"""Drop documents that cannot carry cognition, before paying to send them.

Pure Python, zero API cost. Filtering is **structural only** — never by
subject matter. That someone treats hobby minutiae with the same analytic
seriousness as politics is itself a cognitive tell, and a topic filter would
delete the evidence for it. What gets dropped is posts with no propositional
content at all: bare acknowledgements, link-drops with no commentary, and very
short standalone fragments with no parent to make sense of them.

A short post *with* context is never dropped. "Completely backwards." is four
words and, given its parent, one of the most revealing documents in a corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Document

# Conservative on purpose. These thresholds are the point where a document
# stops being able to contain an argument, not the point where it stops being
# interesting.
MIN_WORDS_NO_CONTEXT = 5
MIN_COMMENTARY_WORDS = 3
MAX_ACK_WORDS = 4

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@\w+")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")

# Pure acknowledgements: the whole body, once stripped, is one of these. Kept
# short and literal — anything longer starts deleting real replies.
ACKNOWLEDGEMENTS = {
    "thanks",
    "thank you",
    "thanks so much",
    "thank you so much",
    "ty",
    "thx",
    "yes",
    "yep",
    "yup",
    "no",
    "nope",
    "agreed",
    "agree",
    "same",
    "true",
    "this",
    "exactly",
    "correct",
    "right",
    "indeed",
    "seconded",
    "ditto",
    "congrats",
    "congratulations",
    "welcome",
    "you're welcome",
    "np",
    "nice",
    "cool",
    "wow",
    "lol",
    "haha",
    "lmao",
    "ha",
    "heh",
    "ok",
    "okay",
    "sorry",
    "oops",
    "done",
    "fixed",
    "noted",
    "sure",
    "of course",
    "amen",
    "rip",
    "sad",
    "yikes",
    "hmm",
    "wat",
    "what",
    "huh",
}


@dataclass
class FilterStats:
    input_documents: int = 0
    kept: int = 0
    dropped: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_documents": self.input_documents,
            "kept": self.kept,
            "dropped": self.dropped,
            "by_reason": dict(self.by_reason),
            "examples": {k: v[:3] for k, v in self.examples.items()},
        }

    def summary_line(self) -> str:
        if not self.dropped:
            return f"{self.kept} documents, none filtered"
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(self.by_reason.items()))
        return f"{self.kept} kept, {self.dropped} filtered ({reasons})"


def _normalized(body: str) -> str:
    text = _URL.sub(" ", body)
    text = _MENTION.sub(" ", text)
    text = _NON_WORD.sub(" ", text.lower())
    return " ".join(text.split())


def classify(doc: Document) -> str | None:
    """Return a drop reason, or None to keep.

    Threads are never dropped: a multi-part post is an argument by construction.
    """
    if doc.part_count > 1:
        return None

    normalized = _normalized(doc.body)
    words = normalized.split()

    if not words:
        # No text at all once links and mentions are gone.
        return "link_only" if doc.outbound_links else "empty"

    if len(words) <= MAX_ACK_WORDS and normalized in ACKNOWLEDGEMENTS:
        return "acknowledgement"

    if doc.outbound_links and len(words) < MIN_COMMENTARY_WORDS:
        return "link_only"

    # A short post with a parent is doing work; a short post without one is a
    # fragment we cannot interpret.
    if doc.context is None and len(words) < MIN_WORDS_NO_CONTEXT:
        return "too_short_no_context"

    return None


def is_substantive_engagement(doc: Document) -> bool:
    """Does this document actually engage with anything?

    The same judgement `classify` makes, asked as a question. A shared link
    with no commentary is a document — it has a URL, a timestamp, and an
    author — but it is not engagement with what it links to, and treating it
    as such is how an axis gets a `weak signal` out of nothing.

    Used by the axis rules in `synthesize.py`, which is why it is exposed
    rather than left implicit in the filter: the filter drops these documents
    before synthesis, but `--no-filter` keeps them and the corpus on disk
    always has them.
    """
    return classify(doc) is None


def filter_low_signal(docs: list[Document]) -> tuple[list[Document], FilterStats]:
    stats = FilterStats(input_documents=len(docs))
    kept: list[Document] = []
    for doc in docs:
        reason = classify(doc)
        if reason is None:
            kept.append(doc)
            continue
        stats.dropped += 1
        stats.by_reason[reason] = stats.by_reason.get(reason, 0) + 1
        stats.examples.setdefault(reason, []).append(doc.source_id)
    stats.kept = len(kept)
    return kept, stats
