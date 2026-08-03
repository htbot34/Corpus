"""Two-stage map-reduce synthesis. One call over 3,000 posts produces mush.

Map:    chronological ~30k-token chunks -> one cheap extraction call each,
        strict JSON out, concurrency capped at 4.
Reduce: one claude-opus-5 call over all map outputs plus signals.json, with
        prompt caching on the corpus block. Reduce is judgment and inference
        with visible reasoning, which is what the expensive model is for.

The hard rules live in the system prompts below and are *also* enforced in
Python afterwards: every evidence id is checked against the real corpus, every
inference is checked for an actual reasoning chain, and anything that fails is
dropped. A rule the model is merely asked to follow is a hope; a rule the code
enforces is a guarantee.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from .axes import AxisSpec, axes_prompt_block, load_axes
from .budget import (
    Budget,
    BudgetExceeded,
    estimate_anthropic_call,
    estimate_tokens,
)
from .models import Axis, Document, MapChunk, Synthesis
from .prefilter import FilterStats, filter_low_signal, is_substantive_engagement
from .tiers import (
    RICH,
    SourceMix,
    TierRules,
    classify_corpus,
    classify_sources,
    prompt_block,
    source_prompt_block,
)

# Map is extraction: find the claims, name the moves, point at the ids. It does
# not need judgment, so it does not need an expensive model — and map is the
# bulk of the tokens.
MAP_MODEL = "claude-haiku-4-5-20251001"
# Reduce builds the generating model and defends every inference. Do not
# downgrade it.
REDUCE_MODEL = "claude-opus-5"

MAP_MAX_TOKENS = 8_000
REDUCE_MAX_TOKENS = 32_000

# `effort` is rejected outright by models that do not implement it — Haiku 4.5
# among them, which is now the default map model. So it is opt-in by model
# rather than opt-out: omitting it is always safe, sending it blindly is a 400
# on every map slice.
EFFORT_CAPABLE_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def supports_effort(model: str) -> bool:
    return model.startswith(EFFORT_CAPABLE_PREFIXES)


# Constrained decoding compiles the JSON schema into a grammar, and the API
# rejects a grammar above an internal size limit with a 400:
#
#   "The compiled grammar is too large, which would cause performance issues."
#
# Measured against the live API on 2026-08-02 by bisecting the then-current
# REDUCE_SCHEMA field by field: 3,522 bytes of schema accepted, 3,809 rejected.
# The schema at the time was 4,826 bytes, so the reduce call could never have
# succeeded — every test passed because FakeAnthropic returns canned JSON and
# never submits the schema anywhere.
#
# The cognition-first REDUCE_SCHEMA is 3,251 bytes and fits, so constrained
# decoding is the normal path again and the grammar cannot emit a markdown
# fence. tests/test_schema_size.py keeps it under budget.
#
# The fallback below stays regardless, because the size limit is not ours to
# control and the next field added could cross it in production, after
# ingestion has been paid for: the reduce asks for constrained decoding, and if
# the schema is refused it retries without `format`, putting the schema in the
# prompt instead. The real guarantee was never the grammar anyway; it is
# `Synthesis.model_validate_json` below, plus the retry-with-the-error loop.
SCHEMA_REJECTION_MARKERS = ("grammar is too large", "compiled grammar", "schema is too large")


def _is_schema_rejection(exc: Exception) -> bool:
    """Did the API refuse the schema itself, as opposed to failing the call?"""
    text = str(exc).lower()
    return any(marker in text for marker in SCHEMA_REJECTION_MARKERS)


CHUNK_TOKEN_TARGET = 30_000
MAP_CONCURRENCY = 4
# Chunking only needs to be approximately right; ~4 chars/token is close enough
# for English prose and costs no API round trip to compute.
CHARS_PER_TOKEN = 4

# Three ids is enough to prove a claim and few enough that the report reads as
# analysis with citations rather than a citation dump with commentary.
EVIDENCE_CAP = 3
# An inference whose reasoning is this short is not a chain, it is a restatement.
MIN_REASONING_CHARS = 80

# Documents of substantive engagement required before an axis may report
# `weak` rather than `none`.
#
# Measured against a real failure. On a live run over @paulg the
# `defense_intel_natsec` axis came back `weak signal` on the strength of one
# glancing exchange about AI influence operations plus a shared link to a .gov
# visa portal — while the coverage block, correctly, said "No documents on
# defense, intelligence, surveillance, or adversary states beyond one glancing
# exchange." The report contradicted itself and the honest half was in the
# caveats.
#
# One tangential mention plus an unrelated URL is `none`. Two is the smallest
# number that can distinguish a position from a thing someone said once, and a
# shared link with no commentary is not engagement at all — see
# `is_substantive_engagement`. This matters more as search adds thinner and
# more varied sources.
MIN_WEAK_DOCUMENTS = 2

MAP_SYSTEM_BASE = """You are extracting structured evidence from one chronological slice of a \
person's public writing. You are not summarizing and not editorializing.

Each document is presented as:

  [id: <document id>] <date> | <kind> | <engagement>
  THEY REPLIED TO (@handle): <verbatim text written by SOMEONE ELSE>
  THEY QUOTED (@handle): <verbatim text written by SOMEONE ELSE>
  THEY WROTE: <the subject's own words>

Only text under THEY WROTE is the subject's. Text under THEY REPLIED TO or THEY \
QUOTED belongs to another person and must never be attributed to the subject. It \
is there so you can tell what they are responding to.

Return strict JSON only:
- topics: recurring subjects in this slice, each with the document ids that show it.
- claims: explicit positions the subject states, each with document ids. Mark a \
claim "amplified_from_others" when the only support is a repost or the content of \
a quoted post rather than the subject's own words. Prefer the claims that reveal \
an underlying commitment over the ones that merely report a fact.
- entities: people, companies, products, papers, and places the subject cites.
- reasoning_moves: how they argue, each with one document id that shows it \
(e.g. "reframes the question", "concedes then narrows", "answers with a \
counterexample", "cites own prior work", "reaches for a base rate").
- highest_signal_document_ids: the 3 documents in this slice that best reveal how \
this person thinks. Not the most popular — the most revealing.

Every id you emit must be an id that appears in this slice. Emit no other prose."""

MAP_AXES_SUFFIX = """

The downstream analysis will try to locate this person on the axes below. When a \
document bears on one of them, make sure it appears in `claims` or \
`highest_signal_document_ids`. Do not force it: a slice that touches none of \
these should return none of them.

{axes}"""


REDUCE_SYSTEM_TEMPLATE = """You are reconstructing how one person thinks from their public \
writing. Not what they post about — the model that generates their positions.

You are given: (a) structured extractions from every chronological slice of their \
corpus, (b) the highest-signal documents verbatim, and (c) signals.json, which \
contains every count and statistic computed from the corpus.

## What each field is for

`core_model` is the centerpiece. Three to six load-bearing beliefs that generate \
the rest — not a list of opinions. For each, `generates` names the surface \
positions that follow from it. A belief that generates nothing is an opinion and \
does not belong here. Mark `role` honestly: "load_bearing" for the roots that \
other positions hang off, "derived" for what follows from a root, \
"held_lightly" for a view they voice but do not defend.

`reasoning` is the machinery: the moves they make, what they will accept as \
evidence, what happens under disagreement, and what it takes for them to update. \
`blind_spots` are patterns they do not appear to see in themselves.

`axes` locates them on the axes listed below. `evolution` is where a view \
actually moved. `open_questions` are the problems they return to without \
resolving. `misreadings` are the wrong conclusions a careless reader would draw \
from this corpus — this is the guard on everything above it, so make it specific \
to this person.

## The two inference tiers, which you must keep apart

- `stated` is what they actually said. Same sourcing rule as always: it traces \
to real document ids or it is dropped.
- `inferred` is what follows from what they said. It requires `reasoning`: the \
chain from specific posts to the conclusion, written out. Naming the conclusion \
again is not a chain. "Their posts suggest this" is not a chain. An inference \
whose reasoning is missing or hand-waving will be deleted before the report is \
written, so write the chain or leave `inferred` empty.

`confidence` describes the inference, not the topic.

## Signal strength, which you must not inflate

`signal: "none"` is a required, valid, expected output. If the corpus contains \
nothing bearing on an axis, say so: set `signal` to "none", leave `stated`, \
`inferred` and `reasoning` empty, and cite nothing. Do not reach for something \
plausible. A confabulated axis is worse than an absent one, because it is \
indistinguishable from a real finding. Most corpora have nothing to say about \
most axes; that is the normal case, not a failure.

"weak" is not "I found something". It requires **at least {weak} documents that \
substantively engage** with the axis. A shared link with no commentary is not \
engagement. A single glancing mention is not engagement. One tangential remark \
plus an unrelated URL is `none`, not `weak` — and it will be corrected to \
`none` in code if you claim otherwise, so claiming it costs you the axis rather \
than winning it.

Use "strong" only when the corpus genuinely lets you locate them.

## Axes for this run

{axes}

{tier}

## Hard rules

1. Every field carrying evidence_ids must reference real document ids from the \
corpus. Drop any finding you cannot source.
2. At most {cap} evidence ids per claim. Choose the most *distinctive* documents, \
not the most recent — the ones that would be hard to explain any other way.
3. Never attribute a parent tweet's or quoted tweet's content to the subject. \
Only THEY WROTE text supports a position.
4. Reposts and quote-tweet targets are the weakest evidence there is. They never \
support a load-bearing belief on their own.
5. All counts come from signals.json. Do not compute or estimate any number.
6. Never manufacture an arc. If no view changed, return an empty `evolution`.

Write `summary` as 3-4 sentences on how this person reasons — the shape of their \
thinking, not a list of their subjects. Return only the JSON object."""


def build_map_system(axes: list[AxisSpec]) -> str:
    if not axes:
        return MAP_SYSTEM_BASE
    return MAP_SYSTEM_BASE + MAP_AXES_SUFFIX.format(axes=axes_prompt_block(axes))


def build_reduce_system(
    axes: list[AxisSpec], tier: TierRules, sources: SourceMix | None = None
) -> str:
    tier_block = prompt_block(tier)
    # How the corpus is spread, appended to how big it is: both are facts
    # about the evidence base, both are counted in Python, and the model is
    # told rather than asked.
    concentration = source_prompt_block(sources) if sources is not None else ""
    if concentration:
        tier_block = f"{tier_block}\n\n{concentration}"
    return REDUCE_SYSTEM_TEMPLATE.format(
        axes=axes_prompt_block(axes),
        tier=tier_block,
        cap=EVIDENCE_CAP,
        weak=MIN_WEAK_DOCUMENTS,
    )


# --------------------------------------------------------------------------
# JSON schemas (hand-written: every field required, additionalProperties false)
# --------------------------------------------------------------------------


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_IDS = {"type": "array", "items": {"type": "string"}}
_STRS = {"type": "array", "items": {"type": "string"}}

MAP_SCHEMA = _obj(
    {
        "topics": _arr(_obj({"name": _STR, "document_ids": _IDS})),
        "claims": _arr(
            _obj(
                {
                    "claim": _STR,
                    "document_ids": _IDS,
                    "confidence": _enum("stated", "implied", "amplified_from_others"),
                }
            )
        ),
        "entities": _STRS,
        "reasoning_moves": _arr(_obj({"move": _STR, "example_id": _STR})),
        "highest_signal_document_ids": _IDS,
    }
)

REDUCE_SCHEMA = _obj(
    {
        "summary": _STR,
        "core_model": _arr(
            _obj(
                {
                    "belief": _STR,
                    "role": _enum("load_bearing", "derived", "held_lightly"),
                    "generates": _STRS,
                    "evidence_ids": _IDS,
                }
            )
        ),
        "reasoning": _obj(
            {
                "moves": _arr(_obj({"move": _STR, "example_id": _STR})),
                "what_counts_as_evidence": _STR,
                "under_disagreement": _STR,
                "updates_when": _STR,
                "blind_spots": _arr(_obj({"pattern": _STR, "basis": _STR, "evidence_ids": _IDS})),
            }
        ),
        "axes": _arr(
            _obj(
                {
                    "axis": _STR,
                    "signal": _enum("strong", "weak", "none"),
                    "stated": _STR,
                    "inferred": _STR,
                    "reasoning": _STR,
                    "confidence": _enum("high", "medium", "low"),
                    "evidence_ids": _IDS,
                }
            )
        ),
        "evolution": _arr(
            _obj(
                {
                    "topic": _STR,
                    "earlier": _STR,
                    "later": _STR,
                    "inflection": _STR,
                    "evidence_ids": _IDS,
                }
            )
        ),
        "open_questions": _arr(_obj({"question": _STR, "returned_to": _INT, "evidence_ids": _IDS})),
        "misreadings": _arr(_obj({"misreading": _STR, "why_wrong": _STR, "evidence_ids": _IDS})),
        "coverage": _obj(
            {
                "date_range": _STR,
                "total_documents": _INT,
                "gaps": _STRS,
                "confidence": _enum("high", "medium", "low"),
            }
        ),
    }
)


def schema_bytes(schema: dict[str, Any]) -> int:
    """Serialized size, measured the way the grammar limit was bisected."""
    return len(json.dumps(schema))


def _output_config(model: str, effort: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Build `output_config`, omitting what the model would reject.

    `schema=None` is the schema-rejection fallback: no `format`, so no grammar.
    """
    config: dict[str, Any] = {}
    if supports_effort(model):
        config["effort"] = effort
    if schema is not None:
        config["format"] = {"type": "json_schema", "schema": schema}
    return config


# --------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Recover bare JSON from a fenced or prose-prefixed response.

    A no-op on the constrained-decoding path, where the grammar physically
    cannot emit a backtick. This exists for the schema-rejection fallback,
    which is exactly where a model asked for JSON in the prompt tends to wrap
    it in ```json — seen live, and it used to cost a full billed generation.
    """
    stripped = text.strip()
    if not stripped:
        return text
    if stripped[0] in "{[":
        return stripped

    match = _FENCE.search(stripped)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner

    # Leading prose, or an unterminated fence: fall back to the outermost
    # brace/bracket pair.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            return stripped[start : end + 1]
    return stripped


def parse_synthesis(raw: str) -> Synthesis:
    return Synthesis.model_validate_json(_strip_code_fences(raw))


def parse_map_chunk(raw: str) -> MapChunk:
    return MapChunk.model_validate_json(_strip_code_fences(raw))


# --------------------------------------------------------------------------
# document rendering
# --------------------------------------------------------------------------


def render_document(doc: Document) -> str:
    """Render one Document for the model.

    Context always appears above the subject's own words and is always labelled
    with whose words they are.
    """
    eng = doc.engagement or {}
    meta = (
        f"[id: {doc.source_id}] {doc.published_at.strftime('%Y-%m-%d')} | {doc.kind}"
        f" | likes={eng.get('likes', 0)} replies={eng.get('replies', 0)}"
        f" reposts={eng.get('reposts', 0)}"
    )
    if doc.part_count > 1:
        meta += f" | thread of {doc.part_count}"
    lines = [meta]
    if doc.context:
        who = f"@{doc.context_author}" if doc.context_author else "someone"
        label = "THEY QUOTED" if doc.kind == "quote" else "THEY REPLIED TO"
        lines.append(f"{label} ({who}): {doc.context}")
    lines.append(f"THEY WROTE: {doc.body}")
    return "\n".join(lines)


def chunk_documents(
    docs: list[Document], token_target: int = CHUNK_TOKEN_TARGET
) -> list[list[Document]]:
    """Bucket chronologically (oldest first) into ~token_target chunks."""
    ordered = sorted(docs, key=lambda d: d.published_at)
    chunks: list[list[Document]] = []
    current: list[Document] = []
    size = 0
    for doc in ordered:
        cost = len(render_document(doc)) // CHARS_PER_TOKEN + 8
        if current and size + cost > token_target:
            chunks.append(current)
            current, size = [], 0
        current.append(doc)
        size += cost
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------


@dataclass
class SynthesisRun:
    synthesis: Synthesis | None = None
    map_outputs: list[dict[str, Any]] = field(default_factory=list)
    chunks: int = 0
    map_failures: int = 0
    dropped_findings: list[str] = field(default_factory=list)
    corrected_counts: list[str] = field(default_factory=list)
    filter_stats: FilterStats | None = None
    analyzed_documents: int = 0
    tier: TierRules | None = None
    sources: SourceMix | None = None
    raw_reduce_output: str = ""
    structured_output: bool = True  # False when the schema was refused
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "map_failures": self.map_failures,
            "dropped_findings": self.dropped_findings,
            "corrected_counts": self.corrected_counts,
            "analyzed_documents": self.analyzed_documents,
            "corpus_tier": self.tier.name if self.tier else "",
            "source_mix": self.sources.counts if self.sources else {},
            "filter": self.filter_stats.as_dict() if self.filter_stats else {},
            "structured_output": self.structured_output,
            "error": self.error,
        }


def _charge(budget: Budget, model: str, message: Any, note: str) -> float:
    """Record actual usage. Returns the cost, for reservation reconciliation."""
    usage = message.usage
    return budget.charge_anthropic(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        note=note,
    )


def _text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


async def _count_input_tokens(client: Any, model: str, system: Any, messages: Any) -> int | None:
    """Exact input-token count via the SDK, or None if unavailable.

    Token counting is free and exact, which beats 4-chars-per-token by enough to
    be worth one round trip before a call that can cost dollars. It is optional
    on purpose: the stub client in tests has no such method, older SDKs did not
    expose it, and a counting failure must degrade to the local estimate rather
    than block a run that was going to succeed.
    """
    counter = getattr(getattr(client, "messages", None), "count_tokens", None)
    if counter is None:
        return None
    try:
        result = await counter(model=model, system=system, messages=messages)
    except Exception:
        return None
    return getattr(result, "input_tokens", None)


async def run_map(
    client: Any,  # AsyncAnthropic, or any stub with the same messages API
    chunks: list[list[Document]],
    budget: Budget,
    *,
    axes: list[AxisSpec] | None = None,
    model: str = MAP_MODEL,
    effort: str = "medium",
    completed: dict[int, dict[str, Any]] | None = None,
    on_slice: Callable[[int, dict[str, Any]], None] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], int]:
    """Map every chunk, skipping any slice a previous attempt already completed.

    `completed` comes from the run manifest. Re-paying the entire map stage to
    retry a reduce that failed validation is the most expensive avoidable
    mistake this tool can make — on a 10,000-post corpus the map stage is ~40
    calls and most of the Anthropic spend.
    """
    semaphore = asyncio.Semaphore(MAP_CONCURRENCY)
    results: list[dict[str, Any] | None] = [None] * len(chunks)
    failures = 0
    system_prompt = build_map_system(axes if axes is not None else load_axes())
    already = completed or {}
    for index, payload in already.items():
        if 0 <= index < len(chunks):
            results[index] = payload
    if already:
        log(f"  reusing {len(already)} map slice(s) from a previous attempt")

    async def one(index: int, docs: list[Document]) -> None:
        nonlocal failures
        if results[index] is not None:
            return  # already paid for
        async with semaphore:
            body = "\n\n---\n\n".join(render_document(d) for d in docs)
            span = (
                f"{docs[0].published_at.strftime('%Y-%m-%d')} to "
                f"{docs[-1].published_at.strftime('%Y-%m-%d')}"
            )
            system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    # Shared across every map call — cache it once.
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            messages = [
                {
                    "role": "user",
                    "content": (
                        f"Slice {index + 1} of {len(chunks)} ({span}), "
                        f"{len(docs)} documents.\n\n{body}"
                    ),
                }
            ]

            # Reserve before the call. Each of the MAP_CONCURRENCY slices in
            # flight holds its own reservation, so four simultaneous calls
            # cannot collectively overshoot — which is exactly what asyncio
            # .gather + charge-on-return allowed before.
            counted = await _count_input_tokens(client, model, system, messages)
            input_tokens = (
                counted if counted is not None else estimate_tokens(system_prompt + str(messages))
            )
            estimate = estimate_anthropic_call(model, input_tokens, MAP_MAX_TOKENS)

            try:
                reservation = budget.reserve(estimate, f"map slice {index + 1}")
            except BudgetExceeded:
                # Not a slice failure — the budget refused it. Let the caller
                # report it as a budget stop, or the report blames the model.
                raise

            try:
                message = await client.messages.create(
                    model=model,
                    max_tokens=MAP_MAX_TOKENS,
                    # The SDK's TypedDicts are stricter than the dict literals
                    # built above. Casting at the call boundary keeps the
                    # prompt-building code readable; the shapes themselves are
                    # pinned by tests/test_sdk_contract.py.
                    system=cast(Any, system),
                    output_config=cast(Any, _output_config(model, effort, MAP_SCHEMA)),
                    messages=cast(Any, messages),
                )
            except BudgetExceeded:
                reservation.settle()
                raise
            except Exception as exc:  # one bad slice must not lose the others
                reservation.settle()
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] FAILED: {exc}")
                return

            actual: float | None = None
            try:
                actual = _charge(budget, model, message, note=f"map slice {index + 1} ({span})")
            finally:
                # Settle after charging: briefly counting both is conservative,
                # counting neither is the overshoot this class exists to prevent.
                reservation.settle(actual)
            if message.stop_reason == "refusal":
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] refused by safety classifier")
                return
            try:
                parsed = parse_map_chunk(_text_of(message))
            except (ValidationError, ValueError) as exc:
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] unparseable JSON: {exc}")
                return
            payload = parsed.model_dump()
            payload["span"] = span
            payload["document_ids"] = [d.source_id for d in docs]
            results[index] = payload
            if on_slice is not None:
                # Checkpoint immediately: the value of a completed slice is that
                # it never has to be paid for twice, which requires it to be on
                # disk before the next thing fails.
                on_slice(index, payload)
            log(
                f"  [map {index + 1}/{len(chunks)}] {span}: "
                f"{len(parsed.topics)} topics, {len(parsed.claims)} claims "
                f"(${budget.total:.4f})"
            )

    try:
        await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
    except BudgetExceeded as exc:
        # Not a failure of the slices — say so, or the report blames the wrong thing.
        log(f"  [budget] map stage stopped: {exc}")

    return [r for r in results if r is not None], failures


def _corpus_block(
    map_outputs: list[dict[str, Any]],
    signals: dict[str, Any],
    highlights: list[Document],
) -> str:
    parts = [
        "## SIGNALS (ground truth — every number in your output must come from here)",
        json.dumps(signals, indent=2, default=str),
        "",
        "## SLICE EXTRACTIONS (chronological)",
        json.dumps(map_outputs, indent=2, default=str),
        "",
        "## HIGHEST-SIGNAL DOCUMENTS (verbatim)",
        "\n\n---\n\n".join(render_document(d) for d in highlights),
    ]
    return "\n".join(parts)


async def run_reduce(
    client: Any,  # AsyncAnthropic, or any stub with the same messages API
    map_outputs: list[dict[str, Any]],
    signals: dict[str, Any],
    highlights: list[Document],
    budget: Budget,
    *,
    axes: list[AxisSpec] | None = None,
    tier: TierRules | None = None,
    sources: SourceMix | None = None,
    model: str = REDUCE_MODEL,
    effort: str = "high",
    log: Callable[[str], None] = print,
) -> tuple[Synthesis | None, str, str, bool]:
    """Returns (synthesis, raw_output, error, used_structured_output)."""
    corpus = _corpus_block(map_outputs, signals, highlights)
    system_prompt = build_reduce_system(
        axes if axes is not None else load_axes(),
        tier if tier is not None else RICH,
        sources,
    )
    attempt_note = ""
    structured = True

    # `attempt` counts *billed* attempts, so the schema-rejection fallback below
    # does not consume one: that retry is free, because the API refuses the
    # schema before generating anything. Two billed attempts, as before.
    attempt = 0
    while attempt < 2:
        attempt += 1
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": corpus,
                # The corpus block is identical on the retry and on `resynth`,
                # so caching it turns the second attempt into a cache read.
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    "Produce the synthesis JSON now."
                    + (f"\n\n{attempt_note}" if attempt_note else "")
                ),
            },
        ]
        if not structured:
            # The schema is no longer enforced by the decoder, so it has to be
            # in the prompt. Stating it verbatim beats describing it.
            content.append(
                {
                    "type": "text",
                    "text": (
                        "Return JSON matching this schema exactly. Every listed "
                        "property is required and no others are permitted. Emit "
                        "the bare JSON object with no markdown fences and no "
                        "prose around it:\n\n" + json.dumps(REDUCE_SCHEMA, indent=2)
                    ),
                }
            )
        messages = [{"role": "user", "content": content}]

        # The single largest exposure in the tool: the reduce model at
        # max_tokens=32_000 is ~$0.80. Charging after the fact means a $10
        # budget at $9.99 fires anyway and lands well past it. Reserve
        # worst-case output; reconcile down when the real usage lands.
        counted = await _count_input_tokens(client, model, system_prompt, messages)
        input_tokens = counted if counted is not None else estimate_tokens(system_prompt + corpus)
        estimate = estimate_anthropic_call(model, input_tokens, REDUCE_MAX_TOKENS)
        log(
            f"  [reduce] attempt {attempt}: ~{input_tokens:,} input tokens, "
            f"reserving ${estimate:.4f} (worst case, {REDUCE_MAX_TOKENS:,} output)"
        )

        # Deliberately NOT caught: a refused reduce must stop the run, not be
        # reported as a model failure. The corpus is already on disk.
        reservation = budget.reserve(estimate, f"reduce attempt {attempt}")

        try:
            async with client.messages.stream(
                model=model,
                max_tokens=REDUCE_MAX_TOKENS,
                system=system_prompt,
                output_config=cast(
                    Any, _output_config(model, effort, REDUCE_SCHEMA if structured else None)
                ),
                messages=cast(Any, messages),
            ) as stream:
                message = await stream.get_final_message()
        except BudgetExceeded:
            reservation.settle()
            raise
        except Exception as exc:
            reservation.settle()
            if structured and _is_schema_rejection(exc):
                # The schema was refused, not the request. Nothing was
                # generated and nothing was billed, so this retry is free.
                structured = False
                attempt -= 1  # nothing was generated and nothing was billed
                log(
                    "  [reduce] the API refused the output schema as too large "
                    "for constrained decoding; falling back to prompt-guided "
                    "JSON, still validated against the pydantic model"
                )
                continue
            return None, "", f"reduce call failed: {exc}", structured

        actual: float | None = None
        try:
            actual = _charge(budget, model, message, note=f"reduce attempt {attempt}")
        finally:
            reservation.settle(actual)
        if message.stop_reason == "refusal":
            return None, "", "reduce refused by safety classifier", structured

        raw = _text_of(message)
        try:
            return parse_synthesis(raw), raw, "", structured
        except (ValidationError, ValueError) as exc:
            log(f"  [reduce] attempt {attempt} failed validation: {exc}")
            if attempt >= 2:
                return None, raw, f"validation failed twice: {exc}", structured
            attempt_note = (
                "Your previous output failed schema validation with this error. "
                "Return corrected JSON matching the schema exactly:\n"
                f"{exc}"
            )
    return None, "", "unreachable", structured


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------


def _is_hand_waving(reasoning: str, conclusion: str) -> bool:
    """True when `reasoning` is not actually a chain.

    Two ways to fail: too short to contain one, or a restatement of the thing
    it is supposed to justify.
    """
    text = " ".join(reasoning.split())
    if len(text) < MIN_REASONING_CHARS:
        return True
    claim = " ".join(conclusion.split()).lower()
    lowered = text.lower()
    return bool(
        claim
        and (
            lowered == claim
            or (len(claim) > 40 and claim in lowered and len(lowered) < len(claim) * 1.3)
        )
    )


def prune_unsourced(
    synthesis: Synthesis,
    valid_ids: set[str],
    axes: list[AxisSpec] | None = None,
    tier: TierRules | None = None,
    documents: dict[str, Document] | None = None,
) -> list[str]:
    """The rules, enforced in code rather than hoped for.

    1. A finding citing ids that do not exist is dropped.
    2. An inference without a real reasoning chain is dropped, exactly as an
       unsourced claim is. The reasoning *is* the evidence for the inference.
    3. Evidence is capped at three ids per claim.
    4. Every requested axis appears exactly once. Missing ones come back as
       `signal: "none"` rather than silently vanishing.
    5. What the corpus is big enough to support, per `tier`. The prompt asks
       for the same thing; this is what makes it true.

    Returns human-readable notes about what was dropped, for the report.
    """
    notes: list[str] = []
    rules = tier if tier is not None else RICH

    def clean(ids: list[str]) -> list[str]:
        seen: list[str] = []
        for i in ids:
            if i in valid_ids and i not in seen:
                seen.append(i)
        return seen[:EVIDENCE_CAP]

    kept_beliefs = []
    for belief in synthesis.core_model:
        belief.evidence_ids = clean(belief.evidence_ids)
        if not belief.evidence_ids:
            notes.append(f"core_model '{belief.belief[:60]}' dropped: no valid evidence ids")
            continue
        # The belief is a sourced claim and survives. `role` and `generates`
        # are claims about where it sits relative to the other beliefs, which
        # is an inference — so the tier cut runs through the entry rather than
        # around it.
        if not rules.allow_belief_structure:
            if belief.generates or belief.role != "unclassified":
                notes.append(
                    f"core_model '{belief.belief[:50]}': structure cleared, {rules.name} "
                    f"corpus ({rules.document_count} documents) cannot place a belief"
                )
            belief.generates = []
            belief.role = "unclassified"
        elif belief.role == "unclassified":
            # Not in the wire schema's enum, so constrained decoding cannot
            # produce it — only the prompt-guided fallback path can. Treat it
            # as an unmade decision rather than letting the model opt out of
            # one the corpus is big enough to support.
            notes.append(
                f"core_model '{belief.belief[:50]}': role 'unclassified' is not the "
                "model's to assign; recorded as derived"
            )
            belief.role = "derived"
        kept_beliefs.append(belief)
    synthesis.core_model = kept_beliefs

    kept_moves = []
    for move in synthesis.reasoning.moves:
        if move.example_id in valid_ids:
            kept_moves.append(move)
        else:
            notes.append(f"reasoning move '{move.move[:50]}' dropped: example id not in corpus")
    synthesis.reasoning.moves = kept_moves

    kept_blind_spots = []
    for spot in synthesis.reasoning.blind_spots:
        spot.evidence_ids = clean(spot.evidence_ids)
        # A blind spot is inference all the way down — there is no sourced half
        # to keep, so a tier that forbids inference drops it whole.
        if not rules.allow_blind_spots:
            notes.append(
                f"blind spot '{spot.pattern[:50]}' dropped: "
                f"{rules.name} corpus ({rules.document_count} documents) cannot establish a pattern"
            )
        elif not spot.evidence_ids:
            notes.append(f"blind spot '{spot.pattern[:50]}' dropped: no valid evidence ids")
        elif len(spot.evidence_ids) < rules.min_chain_documents:
            notes.append(
                f"blind spot '{spot.pattern[:50]}' dropped: {len(spot.evidence_ids)} sourced "
                f"document(s), {rules.name} corpus requires {rules.min_chain_documents}"
            )
        elif _is_hand_waving(spot.basis, spot.pattern):
            notes.append(f"blind spot '{spot.pattern[:50]}' dropped: basis is not a chain")
        else:
            kept_blind_spots.append(spot)
    synthesis.reasoning.blind_spots = kept_blind_spots

    synthesis.axes = _enforce_axes(synthesis.axes, clean, axes, rules, notes, documents)

    kept_evolution = []
    for evo in synthesis.evolution:
        evo.evidence_ids = clean(evo.evidence_ids)
        if not rules.allow_evolution:
            notes.append(
                f"evolution entry '{evo.topic}' dropped: {rules.name} corpus "
                f"({rules.document_count} documents) cannot separate a before from an after"
            )
        elif evo.evidence_ids:
            kept_evolution.append(evo)
        else:
            notes.append(f"evolution entry '{evo.topic}' dropped: no valid evidence ids")
    synthesis.evolution = kept_evolution

    kept_questions = []
    for question in synthesis.open_questions:
        question.evidence_ids = clean(question.evidence_ids)
        if question.evidence_ids:
            kept_questions.append(question)
        else:
            notes.append(f"open question '{question.question[:50]}' dropped: no valid evidence ids")
    synthesis.open_questions = kept_questions

    kept_misreadings = []
    for entry in synthesis.misreadings:
        entry.evidence_ids = clean(entry.evidence_ids)
        if entry.evidence_ids:
            kept_misreadings.append(entry)
        else:
            notes.append(f"misreading '{entry.misreading[:50]}' dropped: no valid evidence ids")
    synthesis.misreadings = kept_misreadings

    return notes


def _blank_axis(name: str) -> Axis:
    return Axis(axis=name, signal="none", confidence="low")


def _drop_inference(axis: Axis, note: str, notes: list[str]) -> None:
    """Delete tier two, keep tier one.

    The stated half has its own sourcing and is not collateral damage when the
    inference fails its test. `confidence` describes the inference, so it goes
    back to the floor along with it.
    """
    axis.inferred = ""
    axis.reasoning = ""
    axis.confidence = "low"
    notes.append(note)


def _substantive_evidence(ids: list[str], documents: dict[str, Document] | None) -> int:
    """How many cited documents actually engage, rather than merely exist.

    Without the corpus to hand — an older caller, a hand-edited synthesis —
    this falls back to counting ids. That still enforces the "at least two
    documents" half of the rule; only the "a shared link is not engagement"
    half needs the documents themselves.
    """
    if documents is None:
        return len(ids)
    return sum(
        1
        for doc_id in ids
        if (doc := documents.get(doc_id)) is not None and is_substantive_engagement(doc)
    )


def _enforce_axes(
    emitted: list[Axis],
    clean: Callable[[list[str]], list[str]],
    requested: list[AxisSpec] | None,
    rules: TierRules,
    notes: list[str],
    documents: dict[str, Document] | None = None,
) -> list[Axis]:
    """Clean each axis, then reconcile against what was actually requested."""
    cleaned: dict[str, Axis] = {}
    for axis in emitted:
        axis.evidence_ids = clean(axis.evidence_ids)

        # A `weak` axis resting on one glancing mention, or on documents that
        # engage with nothing, is `none`. The report used to be able to claim a
        # weak signal in the body while the coverage block said there was
        # nothing there — two true-sounding statements contradicting each
        # other, with the honest one buried in the caveats.
        if axis.signal == "weak":
            substantive = _substantive_evidence(axis.evidence_ids, documents)
            if substantive < MIN_WEAK_DOCUMENTS:
                notes.append(
                    f"axis '{axis.axis}' demoted weak -> none: {substantive} document(s) of "
                    f"substantive engagement, {MIN_WEAK_DOCUMENTS} required "
                    f"(a shared link with no commentary does not count)"
                )
                axis.signal = "none"

        if axis.signal == "none":
            # An axis with no signal carries no content, by definition. Anything
            # the model wrote here is invention with nothing behind it.
            if axis.stated or axis.inferred or axis.reasoning:
                notes.append(f"axis '{axis.axis}': signal none, cleared invented content")
            axis.stated = axis.inferred = axis.reasoning = ""
            axis.confidence = "low"
        elif not axis.evidence_ids:
            notes.append(f"axis '{axis.axis}' demoted to signal none: no valid evidence ids")
            axis.signal = "none"
            axis.stated = axis.inferred = axis.reasoning = ""
            axis.confidence = "low"
        elif axis.inferred and not rules.allow_inference:
            _drop_inference(
                axis,
                f"axis '{axis.axis}': inference suppressed, {rules.name} corpus "
                f"({rules.document_count} documents) supports stated positions only",
                notes,
            )
        elif axis.inferred and len(axis.evidence_ids) < rules.min_chain_documents:
            _drop_inference(
                axis,
                f"axis '{axis.axis}': inference dropped, chain rests on "
                f"{len(axis.evidence_ids)} sourced document(s) and the {rules.name} "
                f"corpus requires {rules.min_chain_documents}",
                notes,
            )
        elif axis.inferred and _is_hand_waving(axis.reasoning, axis.inferred):
            _drop_inference(
                axis,
                f"axis '{axis.axis}': inference dropped, reasoning was not a chain",
                notes,
            )

        if axis.axis in cleaned:
            notes.append(f"axis '{axis.axis}': duplicate entry discarded")
            continue
        cleaned[axis.axis] = axis

    if requested is None:
        return list(cleaned.values())

    out: list[Axis] = []
    for spec in requested:
        if spec.name in cleaned:
            out.append(cleaned.pop(spec.name))
            continue
        notes.append(f"axis '{spec.name}' was not returned; recorded as no signal")
        out.append(_blank_axis(spec.name))
    for name in cleaned:
        notes.append(f"axis '{name}' dropped: not among the requested axes")
    return out


def enforce_signal_counts(
    synthesis: Synthesis,
    signals: dict[str, Any],
    analyzed_documents: int,
    tier: TierRules | None = None,
) -> list[str]:
    """Any number we have ground truth for is overwritten.

    The prompt tells the model not to compute counts. This makes it true.
    `open_questions.returned_to` is left alone: it counts a model-defined
    concept, so signals.json has no counterpart to check it against.

    `coverage.confidence` is a self-assessment, which is exactly the thing a
    thin corpus is bad at — so at that tier it is set rather than believed.
    """
    notes: list[str] = []
    rules = tier if tier is not None else RICH

    if synthesis.coverage.total_documents != analyzed_documents:
        notes.append(
            f"coverage.total_documents corrected "
            f"{synthesis.coverage.total_documents} -> {analyzed_documents}"
        )
        synthesis.coverage.total_documents = analyzed_documents

    date_range = signals.get("date_range", "")
    if date_range and synthesis.coverage.date_range != date_range:
        notes.append(f"coverage.date_range corrected to {date_range}")
        synthesis.coverage.date_range = date_range

    forced = rules.forced_confidence
    if forced is not None and synthesis.coverage.confidence != forced:
        notes.append(
            f"coverage.confidence forced {synthesis.coverage.confidence} -> {forced} "
            f"by the {rules.name} corpus rule"
        )
        synthesis.coverage.confidence = forced

    return notes


def select_highlights(
    docs: list[Document], map_outputs: list[dict[str, Any]], cap: int = 60
) -> list[Document]:
    by_id = {d.source_id: d for d in docs}
    picked: list[Document] = []
    for out in map_outputs:
        for doc_id in out.get("highest_signal_document_ids", []):
            doc = by_id.get(str(doc_id))
            if doc is not None and doc not in picked:
                picked.append(doc)
    picked.sort(key=lambda d: d.published_at)
    return picked[:cap]


async def synthesize(
    docs: list[Document],
    signals: dict[str, Any],
    budget: Budget,
    *,
    api_key: str | None = None,
    axes: list[AxisSpec] | None = None,
    map_model: str = MAP_MODEL,
    reduce_model: str = REDUCE_MODEL,
    map_effort: str = "medium",
    reduce_effort: str = "high",
    prefilter: bool = True,
    client: Any | None = None,
    completed_slices: dict[int, dict[str, Any]] | None = None,
    on_slice: Callable[[int, dict[str, Any]], None] | None = None,
    log: Callable[[str], None] = print,
) -> SynthesisRun:
    """Run map+reduce. Pass `client` to iterate on prompts against a stub
    instead of paying per attempt."""
    run = SynthesisRun()
    selected_axes = axes if axes is not None else load_axes()
    # Reposts and media-only posts are excluded from synthesis input; they are
    # already counted in signals.json.
    synth_docs = [d for d in docs if d.kind in ("original", "thread", "reply", "quote")]

    if prefilter:
        synth_docs, stats = filter_low_signal(synth_docs)
        run.filter_stats = stats
        log(f"  prefilter: {stats.summary_line()}")

    run.analyzed_documents = len(synth_docs)
    # Classified from what the model will actually see, not from the corpus on
    # disk: 45 documents that are really 30 once the acknowledgements are gone
    # is precisely the case the tiers exist to catch. Recorded on the run even
    # when there is nothing to synthesize, so the caller can still report it.
    tier = classify_corpus(run.analyzed_documents)
    run.tier = tier
    # Counted on what the model will see, for the same reason the tier is.
    run.sources = classify_sources([d.source for d in synth_docs])
    if not synth_docs:
        run.error = "no synthesizable documents"
        return run

    # Fail fast and say why. Without this, a missing key surfaces as every map
    # slice "failing", which reads like a data problem instead of a setup one.
    if client is None and not (
        api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        run.error = (
            "ANTHROPIC_API_KEY is not set, so synthesis cannot run. The corpus and "
            "signals are still written; set the key and use `corpus resynth <dir>` "
            "to synthesize without re-fetching."
        )
        log(f"  {run.error}")
        return run

    chunks = chunk_documents(synth_docs)
    run.chunks = len(chunks)
    log(f"  {len(synth_docs)} documents -> {len(chunks)} map slices ({map_model})")
    log(f"  corpus tier: {tier.name} ({tier.document_count} documents)")
    if run.sources is not None and run.sources.is_concentrated:
        log(
            f"    {run.sources.share:.0%} of the corpus is {run.sources.dominant}; "
            f"the reduce prompt says so and constrains what it may generalise to"
        )
    if tier.suppresses_inference:
        log("    inference suppressed; stated positions only")
    elif tier.min_chain_documents > 1:
        log(f"    inference requires {tier.min_chain_documents} sourced documents per chain")

    owns_client = client is None
    if client is None:
        client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
    try:
        run.map_outputs, run.map_failures = await run_map(
            client,
            chunks,
            budget,
            axes=selected_axes,
            model=map_model,
            effort=map_effort,
            completed=completed_slices,
            on_slice=on_slice,
            log=log,
        )
        if not run.map_outputs:
            run.error = (
                f"budget exhausted during the map stage (spent ${budget.total:.4f} of "
                f"${budget.limit:.2f}); no slices completed"
                if budget.stopped
                else "every map slice failed; not attempting reduce"
            )
            return run
        if budget.stopped:
            # Hard stop means hard stop: do not spend on reduce. The map
            # outputs are already paid for and are written out regardless.
            run.error = (
                f"budget exhausted after {len(run.map_outputs)} of {len(chunks)} map "
                f"slices; reduce not attempted"
            )
            return run

        highlights = select_highlights(synth_docs, run.map_outputs)
        log(
            f"  reduce over {len(run.map_outputs)} slices + {len(highlights)} "
            f"highlights ({reduce_model})"
        )
        synthesis, raw, error, structured = await run_reduce(
            client,
            run.map_outputs,
            signals,
            highlights,
            budget,
            axes=selected_axes,
            tier=tier,
            sources=run.sources,
            model=reduce_model,
            effort=reduce_effort,
            log=log,
        )
        run.raw_reduce_output = raw
        run.structured_output = structured
        run.error = error
        if not structured:
            log(
                "  [reduce] completed without constrained decoding; output was "
                "validated in Python instead"
            )
        if synthesis is not None:
            valid_ids = {d.source_id for d in docs}
            run.dropped_findings = prune_unsourced(
                synthesis,
                valid_ids,
                selected_axes,
                tier,
                # The whole corpus, not the post-filter slice: the model can
                # only cite what it saw, but under --no-filter what it saw
                # includes the link-only posts, and those are exactly the
                # documents the weak-signal rule exists to discount.
                documents={d.source_id: d for d in docs},
            )
            for note in run.dropped_findings:
                log(f"  [unsourced] {note}")
            run.corrected_counts = enforce_signal_counts(
                synthesis, signals, run.analyzed_documents, tier
            )
            for note in run.corrected_counts:
                log(f"  [counts] {note}")
            run.synthesis = synthesis
    except BudgetExceeded as exc:
        run.error = f"budget exhausted during synthesis: {exc}"
        log(f"  [budget] {exc}")
    finally:
        if owns_client:
            await client.close()

    return run
