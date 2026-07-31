"""Two-stage map-reduce synthesis. One call over 3,000 posts produces mush.

Map:    chronological ~30k-token chunks -> one claude-sonnet-5 call each,
        strict JSON out, concurrency capped at 4.
Reduce: one claude-opus-5 call over all map outputs plus signals.json, with
        prompt caching on the corpus block.

The hard rules live in the system prompts below and are *also* enforced in
Python afterwards: every evidence id is checked against the real corpus and
unsourceable findings are dropped. A rule the model is merely asked to follow
is a hope; a rule the code enforces is a guarantee.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from .budget import Budget, BudgetExceeded
from .models import Document, MapChunk, Synthesis

MAP_MODEL = "claude-sonnet-5"
REDUCE_MODEL = "claude-opus-5"

CHUNK_TOKEN_TARGET = 30_000
MAP_CONCURRENCY = 4
# Chunking only needs to be approximately right; ~4 chars/token is close enough
# for English prose and costs no API round trip to compute.
CHARS_PER_TOKEN = 4

MAP_SYSTEM = """You are extracting structured evidence from one chronological slice of a \
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
a quoted post rather than the subject's own words.
- entities: people, companies, products, papers, and places the subject cites.
- argumentative_moves: how they argue (e.g. "reframes the question", "concedes then \
narrows", "answers with a counterexample", "cites own prior work").
- highest_signal_document_ids: the 3 documents in this slice that best reveal how \
this person thinks. Not the most popular — the most revealing.

Every id you emit must be an id that appears in this slice. Emit no other prose."""

REDUCE_SYSTEM = """You are producing a sourced synthesis of one person's public \
writing. You are given: (a) structured extractions from every chronological slice \
of their corpus, (b) the highest-signal documents verbatim, and (c) signals.json, \
which contains every count and statistic computed from the corpus.

Hard rules:

1. Every field carrying evidence_ids must reference real document ids from the \
corpus. Drop any finding you cannot source. No unsourced assertions.
2. Never attribute a parent tweet's or quoted tweet's content to the subject. Only \
THEY WROTE text supports a position.
3. Reposts and quote-tweet targets support confidence "amplified_from_others" and \
nothing stronger.
4. All counts come from signals.json. Do not compute or estimate any number. If a \
number you want is not in signals.json, omit the claim rather than inventing it.
5. `evolution` and `performance_gap` are the differentiating fields. If the corpus \
genuinely shows no change of view, return an empty evolution list. If posting \
subject and traction subject match, return an empty performance_gap. Never \
manufacture an arc.
6. `hooks` must be impossible to write without having read this specific corpus. \
Each opener must reference a specific claim, exchange, or reversal in it. Reject \
anything that would apply to any founder or any writer.
7. Thin evidence gets "low_evidence": true, never padding. Two documents is thin.

Write `summary` as exactly 3 sentences, no hedging. Return only the JSON object."""


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


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_IDS = {"type": "array", "items": {"type": "string"}}
_STRS = {"type": "array", "items": {"type": "string"}}

MAP_SCHEMA = _obj(
    {
        "topics": {
            "type": "array",
            "items": _obj({"name": _STR, "document_ids": _IDS}),
        },
        "claims": {
            "type": "array",
            "items": _obj(
                {
                    "claim": _STR,
                    "document_ids": _IDS,
                    "confidence": {
                        "type": "string",
                        "enum": ["stated", "implied", "amplified_from_others"],
                    },
                }
            ),
        },
        "entities": _STRS,
        "argumentative_moves": _STRS,
        "highest_signal_document_ids": _IDS,
    }
)

REDUCE_SCHEMA = _obj(
    {
        "summary": _STR,
        "themes": {
            "type": "array",
            "items": _obj(
                {
                    "name": _STR,
                    "post_count": _INT,
                    "share_of_corpus": _NUM,
                    "first_seen": _STR,
                    "last_seen": _STR,
                    "trajectory": {
                        "type": "string",
                        "enum": ["rising", "steady", "declining", "abandoned"],
                    },
                    "evidence_ids": _IDS,
                    "low_evidence": _BOOL,
                }
            ),
        },
        "positions": {
            "type": "array",
            "items": _obj(
                {
                    "claim": _STR,
                    "confidence": {
                        "type": "string",
                        "enum": ["stated", "implied", "amplified_from_others"],
                    },
                    "evidence_ids": _IDS,
                    "contradicted_by_ids": _IDS,
                    "low_evidence": _BOOL,
                }
            ),
        },
        "argument_style": _obj(
            {
                "typical_moves": _STRS,
                "how_they_handle_disagreement": _STR,
                "evidence_ids": _IDS,
                "low_evidence": _BOOL,
            }
        ),
        "network": {
            "type": "array",
            "items": _obj(
                {
                    "handle": _STR,
                    "exchange_count": _INT,
                    "relationship": _STR,
                    "evidence_ids": _IDS,
                }
            ),
        },
        "reading_diet": {
            "type": "array",
            "items": _obj(
                {"domain": _STR, "share_count": _INT, "what_it_suggests": _STR}
            ),
        },
        "evolution": {
            "type": "array",
            "items": _obj(
                {
                    "topic": _STR,
                    "earlier_view": _STR,
                    "later_view": _STR,
                    "inflection_date": _STR,
                    "evidence_ids": _IDS,
                    "low_evidence": _BOOL,
                }
            ),
        },
        "performance_gap": _obj(
            {
                "posts_most_about": _STR,
                "gets_most_traction_on": _STR,
                "interpretation": _STR,
                "evidence_ids": _IDS,
                "low_evidence": _BOOL,
            }
        ),
        "open_loops": {
            "type": "array",
            "items": _obj(
                {"question": _STR, "returned_to_count": _INT, "evidence_ids": _IDS}
            ),
        },
        "voice": _obj(
            {"register": _STR, "hobbyhorses": _STRS, "tells": _STRS}
        ),
        "hooks": {
            "type": "array",
            "items": _obj({"opener": _STR, "anchor_url": _STR, "why_it_works": _STR}),
        },
        "avoid": {
            "type": "array",
            "items": _obj(
                {"topic_or_framing": _STR, "reason": _STR, "evidence_ids": _IDS}
            ),
        },
        "coverage": _obj(
            {
                "date_range": _STR,
                "total_documents": _INT,
                "kinds_included": _STRS,
                "gaps": _STRS,
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            }
        ),
    }
)


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
    raw_reduce_output: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "map_failures": self.map_failures,
            "dropped_findings": self.dropped_findings,
            "corrected_counts": self.corrected_counts,
            "error": self.error,
        }


def _charge(budget: Budget, model: str, message: Any, note: str) -> None:
    usage = message.usage
    budget.charge_anthropic(
        model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        note=note,
    )


def _text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


async def run_map(
    client: AsyncAnthropic,
    chunks: list[list[Document]],
    budget: Budget,
    *,
    effort: str = "medium",
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], int]:
    semaphore = asyncio.Semaphore(MAP_CONCURRENCY)
    results: list[dict[str, Any] | None] = [None] * len(chunks)
    failures = 0

    async def one(index: int, docs: list[Document]) -> None:
        nonlocal failures
        async with semaphore:
            body = "\n\n---\n\n".join(render_document(d) for d in docs)
            span = (
                f"{docs[0].published_at.strftime('%Y-%m-%d')} to "
                f"{docs[-1].published_at.strftime('%Y-%m-%d')}"
            )
            try:
                message = await client.messages.create(
                    model=MAP_MODEL,
                    max_tokens=8000,
                    system=[
                        {
                            "type": "text",
                            "text": MAP_SYSTEM,
                            # Shared across every map call — cache it once.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    output_config={
                        "effort": effort,
                        "format": {"type": "json_schema", "schema": MAP_SCHEMA},
                    },
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Slice {index + 1} of {len(chunks)} ({span}), "
                                f"{len(docs)} documents.\n\n{body}"
                            ),
                        }
                    ],
                )
            except BudgetExceeded:
                raise
            except Exception as exc:  # one bad slice must not lose the others
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] FAILED: {exc}")
                return

            _charge(budget, MAP_MODEL, message, note=f"map slice {index + 1} ({span})")
            if message.stop_reason == "refusal":
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] refused by safety classifier")
                return
            try:
                parsed = MapChunk.model_validate_json(_text_of(message))
            except (ValidationError, ValueError) as exc:
                failures += 1
                log(f"  [map {index + 1}/{len(chunks)}] unparseable JSON: {exc}")
                return
            payload = parsed.model_dump()
            payload["span"] = span
            payload["document_ids"] = [d.source_id for d in docs]
            results[index] = payload
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
    client: AsyncAnthropic,
    map_outputs: list[dict[str, Any]],
    signals: dict[str, Any],
    highlights: list[Document],
    budget: Budget,
    *,
    effort: str = "high",
    log: Callable[[str], None] = print,
) -> tuple[Synthesis | None, str, str]:
    """Returns (synthesis, raw_output, error)."""
    corpus = _corpus_block(map_outputs, signals, highlights)
    attempt_note = ""

    for attempt in (1, 2):
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
        try:
            async with client.messages.stream(
                model=REDUCE_MODEL,
                max_tokens=32_000,
                system=REDUCE_SYSTEM,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": REDUCE_SCHEMA},
                },
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = await stream.get_final_message()
        except BudgetExceeded:
            raise
        except Exception as exc:
            return None, "", f"reduce call failed: {exc}"

        _charge(budget, REDUCE_MODEL, message, note=f"reduce attempt {attempt}")
        if message.stop_reason == "refusal":
            return None, "", "reduce refused by safety classifier"

        raw = _text_of(message)
        try:
            return Synthesis.model_validate_json(raw), raw, ""
        except (ValidationError, ValueError) as exc:
            log(f"  [reduce] attempt {attempt} failed validation: {exc}")
            if attempt == 2:
                return None, raw, f"validation failed twice: {exc}"
            attempt_note = (
                "Your previous output failed schema validation with this error. "
                "Return corrected JSON matching the schema exactly:\n"
                f"{exc}"
            )
    return None, "", "unreachable"


def prune_unsourced(synthesis: Synthesis, valid_ids: set[str]) -> list[str]:
    """Rule 1, enforced in code: drop findings that cite ids not in the corpus.

    Returns human-readable notes about what was dropped, for the report.
    """
    notes: list[str] = []

    def clean(ids: list[str]) -> list[str]:
        return [i for i in ids if i in valid_ids]

    kept_themes = []
    for theme in synthesis.themes:
        theme.evidence_ids = clean(theme.evidence_ids)
        if theme.evidence_ids:
            kept_themes.append(theme)
        else:
            notes.append(f"theme '{theme.name}' dropped: no valid evidence ids")
    synthesis.themes = kept_themes

    kept_positions = []
    for pos in synthesis.positions:
        pos.evidence_ids = clean(pos.evidence_ids)
        pos.contradicted_by_ids = clean(pos.contradicted_by_ids)
        if pos.evidence_ids:
            kept_positions.append(pos)
        else:
            notes.append(f"position '{pos.claim[:60]}' dropped: no valid evidence ids")
    synthesis.positions = kept_positions

    synthesis.argument_style.evidence_ids = clean(synthesis.argument_style.evidence_ids)

    kept_evolution = []
    for evo in synthesis.evolution:
        evo.evidence_ids = clean(evo.evidence_ids)
        if evo.evidence_ids:
            kept_evolution.append(evo)
        else:
            notes.append(f"evolution entry '{evo.topic}' dropped: no valid evidence ids")
    synthesis.evolution = kept_evolution

    synthesis.performance_gap.evidence_ids = clean(synthesis.performance_gap.evidence_ids)
    if not synthesis.performance_gap.evidence_ids and synthesis.performance_gap.posts_most_about:
        notes.append("performance_gap cleared: no valid evidence ids")
        synthesis.performance_gap.posts_most_about = ""
        synthesis.performance_gap.gets_most_traction_on = ""
        synthesis.performance_gap.interpretation = ""

    for edge in synthesis.network:
        edge.evidence_ids = clean(edge.evidence_ids)
    for loop in synthesis.open_loops:
        loop.evidence_ids = clean(loop.evidence_ids)
    for entry in synthesis.avoid:
        entry.evidence_ids = clean(entry.evidence_ids)

    return notes


def enforce_signal_counts(
    synthesis: Synthesis, signals: dict[str, Any], docs: list[Document]
) -> list[str]:
    """Rule 4, enforced in code: any number we have ground truth for is overwritten.

    The prompt tells the model not to compute counts. This makes it true. Only
    fields with a real counterpart in signals.json are touched — theme post
    counts are inherently model-assigned and are left alone.
    """
    notes: list[str] = []

    if synthesis.coverage.total_documents != len(docs):
        notes.append(
            f"coverage.total_documents corrected {synthesis.coverage.total_documents} -> {len(docs)}"
        )
        synthesis.coverage.total_documents = len(docs)

    date_range = signals.get("date_range", "")
    if date_range and synthesis.coverage.date_range != date_range:
        notes.append(f"coverage.date_range corrected to {date_range}")
        synthesis.coverage.date_range = date_range

    truth = {g["handle"].lower(): g["exchange_count"] for g in signals.get("conversation_graph", [])}
    for edge in synthesis.network:
        actual = truth.get(edge.handle.lstrip("@").lower())
        if actual is not None and edge.exchange_count != actual:
            notes.append(
                f"network[@{edge.handle}].exchange_count corrected "
                f"{edge.exchange_count} -> {actual}"
            )
            edge.exchange_count = actual

    domains = {d["domain"].lower(): d["share_count"] for d in signals.get("outbound_domains", [])}
    for entry in synthesis.reading_diet:
        actual = domains.get(entry.domain.lower())
        if actual is not None and entry.share_count != actual:
            notes.append(
                f"reading_diet[{entry.domain}].share_count corrected "
                f"{entry.share_count} -> {actual}"
            )
            entry.share_count = actual

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
    map_effort: str = "medium",
    reduce_effort: str = "high",
    client: Any | None = None,
    log: Callable[[str], None] = print,
) -> SynthesisRun:
    """Run map+reduce. Pass `client` to iterate on prompts against a stub
    instead of paying per attempt."""
    run = SynthesisRun()
    # Reposts and media-only posts are excluded from synthesis input; they are
    # already counted in signals.json.
    synth_docs = [d for d in docs if d.kind in ("original", "thread", "reply", "quote")]
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
    log(f"  {len(synth_docs)} documents -> {len(chunks)} map slices")

    owns_client = client is None
    if client is None:
        client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
    try:
        run.map_outputs, run.map_failures = await run_map(
            client, chunks, budget, effort=map_effort, log=log
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
        log(f"  reduce over {len(run.map_outputs)} slices + {len(highlights)} highlights")
        synthesis, raw, error = await run_reduce(
            client,
            run.map_outputs,
            signals,
            highlights,
            budget,
            effort=reduce_effort,
            log=log,
        )
        run.raw_reduce_output = raw
        run.error = error
        if synthesis is not None:
            valid_ids = {d.source_id for d in docs}
            run.dropped_findings = prune_unsourced(synthesis, valid_ids)
            for note in run.dropped_findings:
                log(f"  [unsourced] {note}")
            run.corrected_counts = enforce_signal_counts(synthesis, signals, docs)
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
