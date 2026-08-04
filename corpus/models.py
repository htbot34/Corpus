"""Core schemas: Document, Thread, Synthesis.

`Document` is the unit everything downstream operates on. The most important
field is `context`: a reply without its parent is noise, and "completely
backwards" means nothing until you know what it answers.

`Synthesis` is cognition-first. It does not catalogue what someone posts about;
it reconstructs the model that generates their positions. Every field earns its
place by telling you how the mind works — anything that only told you what the
mouth said has been cut.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

DocKind = Literal["original", "thread", "reply", "quote", "repost", "media_only"]

#: What `published_at` holds when the real date could not be established.
#: The epoch rather than the fetch time, because under the newest-first sort a
#: document of unknown date must never surface as the most recent thing the
#: subject wrote — a clock is not a publication date. `date_unknown` is the
#: authoritative flag: code branches on it, never on this value.
DATE_UNKNOWN = datetime(1970, 1, 1, tzinfo=timezone.utc)

# How sure are we that this document was written by the person we are reading?
#
# The failure mode this exists to prevent: automated search on a name returns
# other people, and a confident, well-formatted report that attributes a
# stranger's blog post to the subject is worse than no report at all, because
# the output is indistinguishable from a correct one. So every document carries
# why we believe it is theirs, and the report shows the mix.
#
# Descending:
#   anchor       a URL or handle the user supplied. Certain.
#   linked       reached by following a link from an anchor — a GitHub bio
#                pointing at a blog. Near-certain, and the most valuable path,
#                because people link their own things.
#   corroborated found by search with enough weighed identity evidence: one
#                strong self-declaration (author metadata, a byline-block link
#                back to an anchor), or independent moderate signals agreeing
#                (employer named on the page plus the role).
#   name_match   the name matched and nothing else. Never ingested by default.
Attribution = Literal["anchor", "linked", "corroborated", "name_match"]

# Descending order, for "is this at least as good as X" comparisons.
ATTRIBUTION_ORDER: tuple[Attribution, ...] = ("anchor", "linked", "corroborated", "name_match")

# The confidence a tier starts at. Discovery may raise it within a tier when
# several independent signals agree, never across one: a corroborated match is
# not promoted to linked by being very corroborated.
ATTRIBUTION_CONFIDENCE: dict[Attribution, float] = {
    "anchor": 1.0,
    "linked": 0.85,
    "corroborated": 0.6,
    "name_match": 0.3,
}

# What a run ingests unless told otherwise. `name_match` is deliberately out:
# it is written to unconfirmed.md for a human to accept or reject.
DEFAULT_INGESTED_ATTRIBUTIONS: tuple[Attribution, ...] = ("anchor", "linked", "corroborated")


class Document(BaseModel):
    source: str  # "x", "substack", "rss", "web", "github", ...
    source_id: str
    url: str
    author_handle: str
    published_at: datetime
    #: True when no publication date could be established for this document.
    #: `published_at` then holds `DATE_UNKNOWN` so the newest-first sort puts
    #: it last, but the flag is the truth: an unknown date is a fact about the
    #: document, and substituting the run date for it corrupted a live run's
    #: entire chronology — slice spans, the date-cluster claim, and both
    #: directions of "what moved".
    date_unknown: bool = False
    kind: DocKind
    body: str  # for threads, all parts concatenated
    context: str | None = None  # parent tweet or quoted tweet text, verbatim
    context_author: str | None = None
    context_url: str | None = None
    engagement: dict[str, int] = Field(default_factory=dict)
    outbound_links: list[str] = Field(default_factory=list)
    part_count: int = 1  # 1 unless stitched thread
    raw: dict[str, Any] = Field(default_factory=dict)

    # Why we believe this is theirs. The default is "anchor" because every
    # document produced before discovery existed came from a handle or URL the
    # user typed — the certain case. A corpus.json written by an older run
    # therefore loads with the right value rather than a plausible-looking one.
    attribution: Attribution = "anchor"
    attribution_basis: str = ""  # human-readable: "linked from github.com/jsmith bio"
    attribution_confidence: float = 1.0

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
            date_unknown=root.date_unknown,
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
            # Every part of a self-reply thread came from the same place as its
            # root, so the root's attribution is the thread's. Dropping it here
            # would silently downgrade the best documents in any X corpus to the
            # field default.
            attribution=root.attribution,
            attribution_basis=root.attribution_basis,
            attribution_confidence=root.attribution_confidence,
        )


# --------------------------------------------------------------------------
# Synthesis output schema (validated against the reduce-stage model output)
# --------------------------------------------------------------------------

# "unclassified" is a value only Python ever writes. It is deliberately absent
# from REDUCE_SCHEMA's enum, so grammar-constrained decoding physically cannot
# produce it — the model has three roles in its vocabulary and this fourth one
# means "code cleared this because the corpus could not support the claim".
#
# It is not the same as "held_lightly": that asserts they voice the view but do
# not defend it, which is itself a claim about the belief. On a thin corpus we
# do not know that either, and forcing it would trade one confabulation for
# another.
BeliefRole = Literal["load_bearing", "derived", "held_lightly", "unclassified"]
SignalStrength = Literal["strong", "weak", "none"]
Confidence = Literal["high", "medium", "low"]


class CoreBelief(BaseModel):
    """One node of the generating model.

    `generates` is what makes this different from a list of opinions: it names
    the surface positions that fall out of the belief, so the report shows a
    tree rather than a pile.

    Both `role` and `generates` are claims about the belief's *place* in the
    model, not about the belief itself. The belief is sourced; where it sits is
    inferred. A corpus too thin to support that inference gets `role:
    "unclassified"` and an empty `generates` — the beliefs survive, the
    structure does not.
    """

    belief: str
    role: BeliefRole = "derived"
    generates: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ReasoningMove(BaseModel):
    move: str
    example_id: str = ""


class BlindSpot(BaseModel):
    """An inference, so it carries `basis` for the same reason `axes` carries
    `reasoning`: the chain is the evidence."""

    pattern: str
    basis: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Reasoning(BaseModel):
    moves: list[ReasoningMove] = Field(default_factory=list)
    what_counts_as_evidence: str = ""
    under_disagreement: str = ""
    updates_when: str = ""
    blind_spots: list[BlindSpot] = Field(default_factory=list)


class Axis(BaseModel):
    """A worldview axis with the two inference tiers held apart.

    `stated` is sourced the same way every claim has always been sourced.
    `inferred` is what follows from it, and is only admissible with a
    `reasoning` chain — an inference whose chain is missing or hand-waving is
    dropped exactly as an unsourced claim is dropped.

    `signal: "none"` is a required, expected output, not a failure. An axis the
    corpus cannot speak to must say so; a confabulated axis is worse than an
    absent one because it is indistinguishable from a real finding.
    """

    axis: str
    signal: SignalStrength = "none"
    stated: str = ""
    inferred: str = ""
    reasoning: str = ""
    confidence: Confidence = "low"
    evidence_ids: list[str] = Field(default_factory=list)


class EvolutionEntry(BaseModel):
    topic: str
    earlier: str = ""
    later: str = ""
    inflection: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    question: str
    returned_to: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class Misreading(BaseModel):
    """The guard against the failure mode the inference tier introduces."""

    misreading: str
    why_wrong: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    date_range: str = ""
    total_documents: int = 0
    gaps: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"


class Synthesis(BaseModel):
    summary: str
    core_model: list[CoreBelief] = Field(default_factory=list)
    reasoning: Reasoning = Field(default_factory=Reasoning)
    axes: list[Axis] = Field(default_factory=list)
    evolution: list[EvolutionEntry] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    misreadings: list[Misreading] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)


class MapChunkTopic(BaseModel):
    name: str
    document_ids: list[str] = Field(default_factory=list)


class MapChunkClaim(BaseModel):
    claim: str
    document_ids: list[str] = Field(default_factory=list)
    confidence: Literal["stated", "implied", "amplified_from_others"] = "stated"


class MapChunkMove(BaseModel):
    """A reasoning move plus the document that shows it.

    The id matters: `reasoning.moves` in the final synthesis must cite one, and
    a move the reduce stage cannot source gets dropped.
    """

    move: str
    example_id: str = ""


class MapChunk(BaseModel):
    """Strict JSON returned by each map-stage call."""

    topics: list[MapChunkTopic] = Field(default_factory=list)
    claims: list[MapChunkClaim] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    reasoning_moves: list[MapChunkMove] = Field(default_factory=list)
    highest_signal_document_ids: list[str] = Field(default_factory=list)
