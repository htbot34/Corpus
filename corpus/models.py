"""Core schemas: Document, Thread, Synthesis.

`Document` is the unit everything downstream operates on. The most important
field is `context`: a reply without its parent is noise, and "completely
backwards" means nothing until you know what it answers.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DocKind = Literal["original", "thread", "reply", "quote", "repost", "media_only"]


class Document(BaseModel):
    source: str  # "x", "substack", "rss", "web"
    source_id: str
    url: str
    author_handle: str
    published_at: datetime
    kind: DocKind
    body: str  # for threads, all parts concatenated
    context: str | None = None  # parent tweet or quoted tweet text, verbatim
    context_author: str | None = None
    context_url: str | None = None
    engagement: dict[str, int] = Field(default_factory=dict)
    outbound_links: list[str] = Field(default_factory=list)
    part_count: int = 1  # 1 unless stitched thread
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.body.split())


class Thread(BaseModel):
    """A run of consecutive self-replies, before it is collapsed into one Document.

    Held separately so the stitcher is testable in isolation and so the root's
    identity (url, timestamp, engagement) is never lost during the collapse.
    """

    root_id: str
    parts: list[Document]

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def collapse(self) -> Document:
        root = self.parts[0]
        merged_links: list[str] = []
        for part in self.parts:
            for link in part.outbound_links:
                if link not in merged_links:
                    merged_links.append(link)
        return Document(
            source=root.source,
            source_id=root.source_id,
            url=root.url,
            author_handle=root.author_handle,
            published_at=root.published_at,
            kind="thread" if len(self.parts) > 1 else root.kind,
            body="\n\n".join(p.body for p in self.parts if p.body.strip()),
            # A thread root can itself be a reply to someone else; keep that context.
            context=root.context,
            context_author=root.context_author,
            context_url=root.context_url,
            # Engagement is summed from the root only — later parts of a thread
            # always under-count and averaging them would understate reach.
            engagement=dict(root.engagement),
            outbound_links=merged_links,
            part_count=len(self.parts),
            raw={"root": root.raw, "part_ids": [p.source_id for p in self.parts]},
        )


# --------------------------------------------------------------------------
# Synthesis output schema (validated against the reduce-stage model output)
# --------------------------------------------------------------------------


class Theme(BaseModel):
    name: str
    post_count: int = 0
    share_of_corpus: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    trajectory: Literal["rising", "steady", "declining", "abandoned"] = "steady"
    evidence_ids: list[str] = Field(default_factory=list)
    low_evidence: bool = False


class Position(BaseModel):
    claim: str
    confidence: Literal["stated", "implied", "amplified_from_others"]
    evidence_ids: list[str] = Field(default_factory=list)
    contradicted_by_ids: list[str] = Field(default_factory=list)
    low_evidence: bool = False


class ArgumentStyle(BaseModel):
    typical_moves: list[str] = Field(default_factory=list)
    how_they_handle_disagreement: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    low_evidence: bool = False


class NetworkEdge(BaseModel):
    handle: str
    exchange_count: int = 0
    relationship: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ReadingDietEntry(BaseModel):
    domain: str
    share_count: int = 0
    what_it_suggests: str = ""


class EvolutionEntry(BaseModel):
    topic: str
    earlier_view: str
    later_view: str
    inflection_date: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    low_evidence: bool = False


class PerformanceGap(BaseModel):
    posts_most_about: str = ""
    gets_most_traction_on: str = ""
    interpretation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    low_evidence: bool = False


class OpenLoop(BaseModel):
    question: str
    returned_to_count: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


# `register` is fixed by the output schema and is not ours to rename; pydantic
# warns that it shadows a BaseModel attribute. The warning is cosmetic and the
# field works, so silence it here rather than leaking noise into every CLI run.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='Field name "register"', category=UserWarning)

    class Voice(BaseModel):
        register: str = ""
        hobbyhorses: list[str] = Field(default_factory=list)
        tells: list[str] = Field(default_factory=list)


class Hook(BaseModel):
    opener: str
    anchor_url: str = ""
    why_it_works: str = ""


class AvoidEntry(BaseModel):
    topic_or_framing: str
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    date_range: str = ""
    total_documents: int = 0
    kinds_included: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class Synthesis(BaseModel):
    summary: str
    themes: list[Theme] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    argument_style: ArgumentStyle = Field(default_factory=ArgumentStyle)
    network: list[NetworkEdge] = Field(default_factory=list)
    reading_diet: list[ReadingDietEntry] = Field(default_factory=list)
    evolution: list[EvolutionEntry] = Field(default_factory=list)
    performance_gap: PerformanceGap = Field(default_factory=PerformanceGap)
    open_loops: list[OpenLoop] = Field(default_factory=list)
    voice: Voice = Field(default_factory=Voice)
    hooks: list[Hook] = Field(default_factory=list)
    avoid: list[AvoidEntry] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)


class MapChunkTopic(BaseModel):
    name: str
    document_ids: list[str] = Field(default_factory=list)


class MapChunkClaim(BaseModel):
    claim: str
    document_ids: list[str] = Field(default_factory=list)
    confidence: Literal["stated", "implied", "amplified_from_others"] = "stated"


class MapChunk(BaseModel):
    """Strict JSON returned by each map-stage call."""

    topics: list[MapChunkTopic] = Field(default_factory=list)
    claims: list[MapChunkClaim] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    argumentative_moves: list[str] = Field(default_factory=list)
    highest_signal_document_ids: list[str] = Field(default_factory=list)
