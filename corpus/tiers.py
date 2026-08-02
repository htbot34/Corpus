"""How much the corpus is allowed to support.

Nothing in the synthesis used to vary with document count, which is backwards:
a thin corpus is exactly where inference is most likely to confabulate, because
the model is asked the same questions with less to answer them from and no
instruction to stop.

The tier is computed in Python from the post-filter document count and injected
as ground truth. The model is never asked to assess its own evidence base —
self-assessment is the thing under suspicion. It gets told which tier it is in
and what that permits, and the same rules are then enforced in
`prune_unsourced` regardless of what it returns.

Boundaries are round numbers chosen for where the failure mode changes, not
from a measurement, and the code says so rather than implying precision it does
not have:

- **thin**, under 40 documents. Not enough to distinguish a position someone
  holds from a thing they happened to say once. Stated positions survive;
  everything inferential is suppressed.
- **moderate**, 40 to 149. Enough for inference, not enough for a single
  striking post to be treated as a pattern — so an inference has to rest on
  three distinct documents instead of one.
- **rich**, 150 and up. The behaviour the report was designed around.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CorpusTier = Literal["thin", "moderate", "rich"]

THIN_BELOW = 40
RICH_FROM = 150


@dataclass(frozen=True)
class TierRules:
    """What a corpus of this size is permitted to support.

    Every field is enforced in Python. The prompt states the same rules so the
    model does not waste a generation writing what will be deleted, but the
    prompt is the courtesy and the enforcement is the guarantee.
    """

    name: CorpusTier
    document_count: int
    #: May `axes[].inferred` and `reasoning.blind_spots` exist at all?
    allow_inference: bool
    #: Distinct sourced documents an inference must rest on. 1 is "any source
    #: at all", which is the rule for every other finding in the report.
    min_chain_documents: int
    #: Patterns someone does not see in themselves need a run of behaviour.
    allow_blind_spots: bool
    #: A change of view needs enough before and after to tell them apart.
    allow_evolution: bool
    #: When set, `coverage.confidence` is overwritten with this.
    forced_confidence: Literal["high", "medium", "low"] | None

    @property
    def suppresses_inference(self) -> bool:
        return not self.allow_inference


THIN = TierRules(
    name="thin",
    document_count=0,
    allow_inference=False,
    min_chain_documents=1,  # unused while inference is off; kept honest anyway
    allow_blind_spots=False,
    allow_evolution=False,
    forced_confidence="low",
)

MODERATE = TierRules(
    name="moderate",
    document_count=0,
    allow_inference=True,
    # Three, deliberately equal to EVIDENCE_CAP: an inference at this scale must
    # rest on every document it is allowed to cite. One striking post is an
    # anecdote; three is the smallest thing that can be called a pattern.
    min_chain_documents=3,
    allow_blind_spots=True,
    allow_evolution=True,
    forced_confidence=None,
)

RICH = TierRules(
    name="rich",
    document_count=0,
    allow_inference=True,
    min_chain_documents=1,
    allow_blind_spots=True,
    allow_evolution=True,
    forced_confidence=None,
)


def classify_corpus(document_count: int) -> TierRules:
    """Tier for a corpus of `document_count` synthesizable documents.

    `document_count` is the **post-filter** count — what the model actually
    sees. Classifying on the pre-filter count would promote a corpus of 45
    documents that is really 30 once the acknowledgements are gone, which is
    precisely the case the tiers exist to catch.
    """
    if document_count < THIN_BELOW:
        base = THIN
    elif document_count < RICH_FROM:
        base = MODERATE
    else:
        base = RICH
    return TierRules(
        name=base.name,
        document_count=document_count,
        allow_inference=base.allow_inference,
        min_chain_documents=base.min_chain_documents,
        allow_blind_spots=base.allow_blind_spots,
        allow_evolution=base.allow_evolution,
        forced_confidence=base.forced_confidence,
    )


THIN_REMEDY = (
    "Raise `--max-posts`, drop `--since`, or widen "
    "`--empty-window-tolerance` to reach further back; and merge in their "
    "long-form writing with `--substack DOMAIN`, `--rss URL`, or `--url URL`, "
    "which cost nothing and land in the same corpus."
)

_PROMPT_BLOCKS: dict[CorpusTier, str] = {
    "thin": """## Corpus size: THIN ({n} documents) — inference is switched off

This is ground truth, computed in Python from the documents you were given. Do
not second-guess it and do not reason about whether it feels right.

At this size you cannot tell a position someone holds from a thing they
happened to say once. So:

- Leave every `axes[].inferred` and every `axes[].reasoning` **empty**. Report
  only what they actually said, in `stated`, sourced as always.
- Return `reasoning.blind_spots` as an **empty list**. A blind spot is a
  pattern, and there is not enough here to establish one.
- Return `evolution` as an **empty list**. A change of view needs enough before
  and after to distinguish, and this corpus does not have it.
- `core_model` and `axes[].stated` are still expected — those are sourced
  claims, not inferences.

Anything inferential you write will be deleted before the report is rendered.
Write the sourced half well instead.""",
    "moderate": """## Corpus size: MODERATE ({n} documents) — inference allowed, three sources each

This is ground truth, computed in Python from the documents you were given.

Inference is permitted, but at this size one striking post is an anecdote, not
a pattern. Every `axes[].inferred` and every `reasoning.blind_spots` entry must
cite **at least {k} distinct documents** in its `evidence_ids`. An inference you
can only source to one or two documents does not survive — drop it, or find the
third document that shows the same thing.

The `reasoning` chain must still be a real chain: the path from those specific
posts to the conclusion, written out.""",
    "rich": """## Corpus size: RICH ({n} documents)

This is ground truth, computed in Python from the documents you were given. The
corpus is large enough for the full analysis. The standing rules apply: an
inference needs a real reasoning chain and at least one sourced document, and
`signal: "none"` remains the right answer wherever the corpus is silent.""",
}


def prompt_block(rules: TierRules) -> str:
    """The tier section injected into the reduce system prompt."""
    return _PROMPT_BLOCKS[rules.name].format(n=rules.document_count, k=rules.min_chain_documents)
