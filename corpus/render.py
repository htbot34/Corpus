"""report.md rendering.

Cognition-first: the generating model up top, then the machinery, then where
the corpus does and does not let you locate them. Evidence is capped at three
links per claim so the page reads as analysis with citations rather than a
citation dump with commentary.

Interpretation of the computed metrics belongs inside the analysis, not in
tables. That someone's most-linked domain is their own site is a fact about
epistemic self-reliance; it is not a table row.

The coverage callout is unchanged from the production-hardening pass: every
caveat it raises (hiatus truncation, unparseable timestamps, the ingested
share, the schema-rejection degrade) is about whether the corpus is
trustworthy, which the redesign did not touch.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import Document, Synthesis
from .synthesize import EVIDENCE_CAP
from .tiers import THIN_BELOW, THIN_REMEDY, TierRules, classify_corpus, classify_sources

ROLE_LABELS = {
    "load_bearing": "load-bearing",
    "derived": "derived",
    "held_lightly": "held lightly",
    "unclassified": "position in the model not assessed",
}

#: Share of documents with unknown dates above which chronology-dependent
#: output is not trusted: "what moved" is suppressed and the confidence label
#: is capped. Evolution analysis on unreliable chronology is worse than no
#: evolution analysis — the simonw-nox run put a Later three weeks before its
#: Earlier and reported it at high confidence.
DATE_QUALITY_FLOOR = 0.10

#: Part one leads with the axes readers ask about first; anything configured
#: beyond these six follows in its original order.
_PART_ONE_AXIS_ORDER = (
    "technology_and_ai",
    "defense_intel_natsec",
    "politics_and_ideology",
    "institutions_and_authority",
    "economics_and_markets",
    "epistemics",
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _first_sentences(text: str, limit: int) -> str:
    """The first `limit` sentences of a passage, joined back together."""
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    return " ".join(parts[:limit])


def _link_map(docs: list[Document]) -> dict[str, Document]:
    return {d.source_id: d for d in docs}


def _cite(ids: list[str], links: dict[str, Document]) -> str:
    """Render evidence ids as markdown links to the source posts.

    A finding resting *only* on corroborated evidence says so. Corroborated
    means "found by search, with enough weighed identity evidence to promote"
    — good enough to ingest, not good enough to let a claim stand on it
    silently. Anchor and linked evidence carry no marker, because the
    unmarked case should be the certain one.

    Capped here as well as in `prune_unsourced`, because `--render-only` runs
    against whatever synthesis.json is on disk — including one edited by hand.
    """
    out = []
    cited: list[Document] = []
    for doc_id in ids[:EVIDENCE_CAP]:
        doc = links.get(doc_id)
        if doc is None:
            continue
        cited.append(doc)
        out.append(f"[{doc.published_at.strftime('%Y-%m-%d')}]({doc.url})")
    if not out:
        return "_no source_"
    if cited and all(d.attribution == "corroborated" for d in cited):
        return ", ".join(out) + " _(corroborated sources only — attribution is not certain)_"
    return ", ".join(out)


def _attribution_line(docs: list[Document]) -> str | None:
    """The coverage block's answer to "how do you know this is them?".

    Silent when every document is an anchor, which is the whole corpus before
    discovery finds anything — a line saying "100% certain" on every report
    would train the reader to skip the one report where it matters.
    """
    counts: dict[str, int] = {}
    for doc in docs:
        counts[doc.attribution] = counts.get(doc.attribution, 0) + 1
    if not counts or set(counts) == {"anchor"}:
        return None
    order = ("anchor", "linked", "corroborated", "name_match")
    parts = [f"{counts[label]} {label.replace('_', ' ')}" for label in order if label in counts]
    line = "- Attribution: " + ", ".join(parts)
    weak = counts.get("corroborated", 0) + counts.get("name_match", 0)
    if weak:
        line += (
            f". **{weak} document(s) were matched rather than supplied**, so the "
            "claims resting on them are only as good as the match."
        )
    return line


def _tier_for(synthesis: Synthesis | None, run_meta: dict[str, Any]) -> TierRules | None:
    """The tier this report was produced under.

    `run_meta` is authoritative. `coverage.total_documents` is the fallback,
    because `--render-only` can meet a directory whose run_meta.json is missing
    — and that field holds the same post-filter count Python classified on, so
    the fallback reproduces the original tier rather than guessing at one.
    """
    analyzed = run_meta.get("analyzed_documents")
    if analyzed is None and synthesis is not None:
        analyzed = synthesis.coverage.total_documents
    if not analyzed:
        return None
    rules = classify_corpus(int(analyzed))
    recorded = run_meta.get("corpus_tier")
    if recorded and recorded != rules.name:
        # Only reachable if run_meta and coverage disagree, which means one of
        # them was edited. Trust what the run recorded.
        return None
    return rules


def _callout(lines: list[str]) -> str:
    return "\n".join(f"> {line}" for line in lines)


def _search_lines(search: dict[str, Any]) -> list[str]:
    """What Phase 2 did, in the coverage block.

    A run that searched and found nothing and a run that never searched look
    identical in a report that only lists what it ingested — and they are very
    different claims about how hard the tool looked.
    """
    if not search or not search.get("queries"):
        return []
    lines = [
        f"- Search: {search.get('searches', 0)} quer(y/ies) run"
        f" ({search.get('cached_searches', 0)} served from cache),"
        f" {search.get('results_seen', 0)} result(s),"
        f" {search.get('verified', 0)} verified,"
        f" {len(search.get('candidates', []))} ingested,"
        f" {len(search.get('held', []))} held"
    ]
    if search.get("common_name"):
        fields = ", ".join(f"`{f}`" for f in search.get("disambiguators", []))
        lines.append(
            "- **The name is too common to resolve by search.** Nothing found by "
            "search was ingested."
            + (f" Adding {fields} to the card would narrow it." if fields else "")
        )
    if search.get("held"):
        # "not confirmed as theirs", not "could not be verified": most of these
        # were fetched and scored, and the scoring is what held them. Saying
        # the fetch failed would blame the wrong thing.
        lines.append(
            f"- {len(search['held'])} search candidate(s) were **not confirmed as theirs** "
            "and are not in this corpus. They are listed in `unconfirmed.md`, with what "
            "matched and what did not. Held means unproved, not rejected."
        )
        # ...and for the rest, the fetch *is* the thing to blame — so say so,
        # with the ceiling. On real search candidates roughly half the pages
        # cannot be read at all, and a reader who does not know that reads
        # "24 held" as "24 rejected".
        unread = sum(1 for c in search["held"] if not c.get("verified"))
        if unread:
            cap = search.get("max_fetches", 0)
            ceiling = f" under this run's fetch ceiling of {cap} page(s)" if cap else ""
            lines.append(
                f"- {unread} of the held candidate(s) were held with their page unread"
                f"{ceiling}. Publishers and aggregators block roughly half of candidate "
                "fetches (12 of 27 pages were readable on a reference run), so an unread "
                "hold says the evidence was never seen — not that it was found wanting."
            )
    if search.get("context"):
        lines.append(
            f"- {len(search['context'])} page(s) found by search are *about* the subject "
            "rather than by them (profiles, interviews, conference bios). They are "
            "recorded in discovery.json and were never treated as their writing."
        )
    return lines


def _fetch_loss_lines(run_meta: dict[str, Any]) -> list[str]:
    """Where reads were lost, per phase, in countable form.

    "56% of pages could not be fetched" was known from one reference run and
    the breakdown behind it had never been recorded anywhere a reader could
    see. A wall of 403s, a wall of timeouts and a wall of 429s are three
    different problems with three different fixes, and a coverage block that
    only says how many reads were lost leaves the reader to guess which one
    they have.
    """
    lines: list[str] = []
    for label, block in (
        ("link-following (phase 1)", run_meta.get("discovery") or {}),
        ("search verification (phase 2)", run_meta.get("search") or {}),
    ):
        failures = block.get("fetch_failures") or {}
        if failures:
            lost = sum(failures.values())
            ordered = sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))
            breakdown = ", ".join(f"{name} × {count}" for name, count in ordered)
            lines.append(
                f"- {lost} page read(s) were lost during {label}: {breakdown}. "
                "Each lost read is a page judged on less evidence or not at all."
            )
        attempts = int(block.get("reader_attempts") or 0)
        if attempts:
            recovered = int(block.get("reader_recoveries") or 0)
            lines.append(
                f"- The reader-service fallback recovered {recovered} of {attempts} "
                "blocked page(s). Recovered pages are extracted text, not original "
                "HTML — authorship signals are weaker there, and each such document "
                "says so in its attribution basis."
            )
    return lines


def _concentration_line(docs: list[Document]) -> str:
    """Say when the corpus is really one source wearing several hats.

    The tiers count documents; this counts where they came from. 200 GitHub
    review comments and 200 documents spread over a blog, a Substack and a
    talk are not the same evidence base, and a report that treats them alike
    invites the reader to generalise past what was read.
    """
    mix = classify_sources([d.source for d in docs])
    if not mix.is_concentrated:
        return ""
    return (
        f"- **{mix.share:.0%} of this corpus is a single source ({mix.dominant}).** "
        f"It speaks to {mix.reach}. Where an axis below reports no signal, that may "
        f"be a fact about the corpus rather than about the subject."
    )


def _ordered_axes(synthesis: Synthesis) -> list[Any]:
    rank = {name: index for index, name in enumerate(_PART_ONE_AXIS_ORDER)}
    return sorted(
        synthesis.axes,
        key=lambda a: rank.get(a.axis, len(_PART_ONE_AXIS_ORDER)),
    )


def _axis_capsule(axis: Any) -> str:
    """One axis in at most four sentences, with the count it rests on.

    A no-signal axis renders at the same visual weight as a verdict. "Nothing
    in this corpus locates them on this axis" is a finding — it is the thing
    that keeps a one-page report honest, and it is never dropped for space.
    """
    label = axis.axis.replace("_", " ")
    if axis.signal == "none":
        return (
            f"**{label}** — no signal, 0 cited documents. "
            "Nothing in this corpus locates them on this axis."
        )
    head = f"**{label}** — {axis.signal} signal, {len(axis.evidence_ids)} cited document(s). "
    stated = _first_sentences(axis.stated, 2)
    inferred = _first_sentences(axis.inferred, 1 if stated else 2)
    parts = []
    if stated:
        parts.append(stated)
    if inferred:
        parts.append(f"Inferred: {inferred}")
    if not parts and axis.reasoning:
        parts.append(_first_sentences(axis.reasoning, 2))
    return head + " ".join(parts)


def _most_limiting_caveat(
    *,
    docs: list[Document],
    unknown: int,
    tier: TierRules | None,
    run_meta: dict[str, Any],
) -> str:
    """The one caveat part one carries. Ordered by how badly each misleads."""
    if docs and unknown / len(docs) > DATE_QUALITY_FLOOR:
        return (
            f"{unknown} of {len(docs)} documents ({unknown / len(docs):.0%}) carry no "
            "reliable publication date, so everything chronological is suspect and "
            '"what moved" is suppressed'
        )
    if run_meta.get("budget_stopped"):
        return "the budget ran out mid-run, so these results are partial"
    if (run_meta.get("ingest") or {}).get("x_failure"):
        return "the X source failed mid-run; its timeline is missing from this corpus"
    if tier is not None and tier.suppresses_inference:
        return f"only {tier.document_count} documents survived filtering; inference is off"
    mix = classify_sources([d.source for d in docs])
    if mix.is_concentrated:
        return f"{mix.share:.0%} of the corpus is a single source ({mix.dominant})"
    if unknown:
        return f"{unknown} document(s) carry no reliable publication date"
    return "none beyond corpus size"


def _part_one(
    *,
    synthesis: Synthesis,
    docs: list[Document],
    signals: dict[str, Any],
    run_meta: dict[str, Any],
    tier: TierRules | None,
    unknown: int,
    confidence_display: str,
) -> list[str]:
    """The verdict page: one page, no more, every claim carrying its count.

    Everything here is drawn from the same synthesis part two lays out in
    full — this is selection and compression, never new analysis.
    """
    out: list[str] = ["## Where they land", ""]

    lead = _first_sentences(synthesis.summary, 3)
    if lead:
        out.append(lead)
        out.append("")

    structured_header = (
        "the-generating-model"
        if (tier is None or tier.allow_belief_structure)
        else "beliefs-without-the-structure"
    )

    for axis in _ordered_axes(synthesis):
        out.append(_axis_capsule(axis))
        out.append("")
    if synthesis.axes:
        out.append("[Full reasoning chains](#the-axes-in-full)")
        out.append("")

    load_bearing = [b for b in synthesis.core_model if b.role == "load_bearing"]
    picked = (load_bearing or synthesis.core_model)[:4]
    if picked:
        out.append(
            "**Load-bearing beliefs**"
            if load_bearing
            else "**Beliefs** (their place in the model was not assessed at this corpus size)"
        )
        out.append("")
        for belief in picked:
            out.append(f"- {belief.belief} ({len(belief.evidence_ids)} cited document(s))")
        out.append("")
        out.append(f"[Full generating model](#{structured_header})")
        out.append("")

    if synthesis.misreadings:
        out.append("**How to misread this**")
        out.append("")
        for entry in synthesis.misreadings[:3]:
            out.append(f"- {entry.misreading} ({len(entry.evidence_ids)} cited document(s))")
        out.append("")
        out.append("[All misreadings, with why each is wrong](#how-to-misread-this)")
        out.append("")

    tier_label = f"{tier.name} corpus" if tier is not None else "corpus tier unknown"
    date_range = synthesis.coverage.date_range or signals.get("date_range", "unknown")
    count = synthesis.coverage.total_documents or len(docs)
    out.append(
        f"**Confidence:** {confidence_display} · {tier_label} · {count} documents · "
        f"{date_range} · Most limiting: "
        f"{_most_limiting_caveat(docs=docs, unknown=unknown, tier=tier, run_meta=run_meta)}."
    )
    out.append("")
    out.append("---")
    out.append("")
    out.append("# The evidence")
    out.append("")
    return out


def _document_count(synthesis: Synthesis | None, docs: list[Document]) -> str:
    """What the header says about how much was read.

    Says "N of M documents" when they differ, rather than picking one and
    leaving the reader to find the other number lower down.
    """
    analyzed = synthesis.coverage.total_documents if synthesis else 0
    if analyzed and analyzed != len(docs):
        return f"{analyzed} of {len(docs)} documents"
    return f"{len(docs)} documents"


def render_report(
    *,
    handle: str,
    synthesis: Synthesis | None,
    docs: list[Document],
    signals: dict[str, Any],
    budget_lines: list[str],
    run_meta: dict[str, Any],
    subject: str | None = None,
) -> str:
    links = _link_map(docs)
    out: list[str] = []
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # `subject` is the person; `handle` is the X account. They are the same
    # thing only when X is the only source, which is no longer the usual case.
    out.append(f"# {subject or '@' + handle} — how they think")
    out.append("")
    # The analyzed count, not the corpus count. The Python correction in
    # `enforce_signal_counts` fires on `coverage.total_documents`, and the
    # header used to read `len(docs)` — so a run where the prefilter dropped
    # four posts printed "94 documents" here and "90 of 94" in the coverage
    # block, two lines apart and disagreeing.
    out.append(
        f"_Generated {generated} · {_document_count(synthesis, docs)} · "
        f"{signals.get('date_range', '')}_"
    )
    out.append("")

    # ---- coverage caveats, at the top of the evidence half ----------------
    caveats: list[str] = ["**Coverage and caveats**", ""]
    tier = _tier_for(synthesis, run_meta)
    cov = synthesis.coverage if synthesis else None
    # Data quality bounds confidence before anything else does: a corpus whose
    # chronology is partly fabricated must not report "high" however many
    # documents it holds.
    unknown = sum(1 for d in docs if d.date_unknown)
    unknown_share = (unknown / len(docs)) if docs else 0.0
    dates_unreliable = unknown_share > DATE_QUALITY_FLOOR
    confidence_display = cov.confidence if cov else "low"
    if cov and dates_unreliable and cov.confidence == "high":
        confidence_display = "medium"
    if cov:
        caveats.append(f"- Date range: {cov.date_range or signals.get('date_range', 'unknown')}")
        caveats.append(
            f"- Documents analyzed: {cov.total_documents or len(docs)} of {len(docs)} in corpus"
        )
        # At a tier that forces confidence, calling it "model-assessed" would be
        # a lie: the model's own assessment is the thing that was overridden.
        # Checked against the value rather than assumed from the tier, because
        # `--render-only` can meet a synthesis.json that never went through
        # `enforce_signal_counts` — and a label that claims code set a number
        # code did not set is the same kind of lie in the other direction.
        forced = (
            tier is not None
            and tier.forced_confidence is not None
            and cov.confidence == tier.forced_confidence
        )
        label = (
            "Confidence (set in code, not by the model)" if forced else "Model-assessed confidence"
        )
        if confidence_display != cov.confidence:
            caveats.append(
                f"- Confidence: **{confidence_display}** — the model assessed "
                f"{cov.confidence}, capped in the render because {unknown} of "
                f"{len(docs)} documents ({unknown_share:.0%}) carry no reliable "
                "publication date. Confidence is bounded by data quality, not "
                "only by document count."
            )
        else:
            caveats.append(f"- {label}: **{confidence_display}**")
        for gap in cov.gaps:
            caveats.append(f"- Gap: {gap}")
    else:
        caveats.append(f"- Date range: {signals.get('date_range', 'unknown')}")
        caveats.append(f"- Documents ingested: {len(docs)}")

    if unknown:
        emphasis = "**" if dates_unreliable else ""
        caveats.append(
            f"- {emphasis}{unknown} of {len(docs)} documents ({unknown_share:.0%}) carry "
            f"no reliable publication date.{emphasis} They are excluded from every date "
            "computation rather than stamped with a guess."
            + (
                ' Chronology-dependent analysis ("what moved") is suppressed.'
                if dates_unreliable
                else ""
            )
        )

    attribution = _attribution_line(docs)
    if attribution:
        caveats.append(attribution)
    held = (run_meta.get("discovery") or {}).get("held") or []
    if held:
        caveats.append(
            f"- {len(held)} further source(s) matched the name and nothing else, and were "
            "**not** ingested. They are listed in discovery.json."
        )
    caveats.extend(_search_lines(run_meta.get("search") or {}))
    caveats.extend(_fetch_loss_lines(run_meta))
    concentration = _concentration_line(docs)
    if concentration:
        caveats.append(concentration)
    # What a source could not give, in the source's own words. A per-platform
    # ceiling — GitHub's events feed reaching back only ~90 days — is a fact
    # about the corpus, and a reader who has to infer it from a suspiciously
    # short date range will infer something about the person instead.
    for note in run_meta.get("source_notes", [])[:6]:
        caveats.append(f"- {note}")

    if tier is not None:
        if tier.suppresses_inference:
            caveats.append(
                f"- **Corpus tier: {tier.name} ({tier.document_count} documents). "
                "Inference is switched off** — see the note below the summary."
            )
        elif tier.min_chain_documents > 1:
            caveats.append(
                f"- Corpus tier: {tier.name} ({tier.document_count} documents). Every "
                f"inference below rests on at least {tier.min_chain_documents} distinct "
                "documents, a stricter bar than a larger corpus would need."
            )
        else:
            caveats.append(f"- Corpus tier: {tier.name} ({tier.document_count} documents).")

    filt = run_meta.get("filter") or {}
    if filt.get("dropped"):
        reasons = ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in sorted(filt.get("by_reason", {}).items())
        )
        caveats.append(
            f"- {filt['dropped']} low-signal document(s) filtered before synthesis "
            f"({reasons}). Structural only — nothing was filtered by subject. "
            "Re-run with `--no-filter` to include them."
        )

    ingest = run_meta.get("ingest", {})
    # A run that lost its X source mid-way must say so before any number
    # below, because every count and every axis is missing that timeline.
    if ingest.get("x_failure"):
        lost = (
            "its timeline is **absent from this corpus**"
            if ingest.get("x_status") == "failed"
            else "its timeline is **incomplete in this corpus**"
        )
        caveats.append(
            f"- **The X source {ingest.get('x_status', 'failed')}** and {lost}: "
            f"{ingest['x_failure']} The run continued on the other sources, and "
            "a later run will resume the X walk from the saved checkpoint."
        )
    if ingest.get("empty_windows"):
        ranges = ", ".join(f"{a}..{b}" for a, b in ingest.get("empty_window_ranges", [])[:6])
        caveats.append(
            f"- {ingest['empty_windows']} time window(s) returned nothing and may be "
            f"provider gaps rather than real silence: {ranges}"
        )
    if ingest.get("integrity_warning"):
        caveats.append(f"- **{ingest['integrity_warning']}**")
    if ingest.get("cursor_repeat_breaks"):
        caveats.append(
            f"- {ingest['cursor_repeat_breaks']} window(s) hit the duplicate-cursor "
            "regression and were cut short; deep history may be thinner than it looks."
        )
    # Phase 2.3: a run cut short by the tolerance rule may be missing years.
    # That belongs at the top of the caveat block in bold, not buried in
    # stop_reason where a reader has to know what it means.
    if ingest.get("stopped_on_tolerance"):
        last = ingest.get("last_date_reached") or "unknown"
        if ingest.get("probe_confirmed_end"):
            # A probe swept a further twelve months and found nothing. Still
            # worth stating plainly — but this is evidence, not a guess, and
            # crying wolf here would devalue the warning below.
            caveats.append(
                f"- Ingestion reached **{last}** and stopped there. A "
                f"12-month probe below that date returned nothing, so this is "
                f"most likely the full public history rather than a truncation."
            )
        else:
            caveats.append(
                f"- **HISTORY MAY BE INCOMPLETE.** Ingestion stopped on the "
                f"consecutive-empty-window rule at **{last}**, not because the "
                f"account ran out of posts. Anything published before {last} is "
                f"absent from this report. Re-run with a larger "
                f"`--empty-window-tolerance` or `--window-days` to reach further "
                f"back."
            )
    # Ingested-vs-total, so "400 of 53,901" is visible at a glance.
    total_known = ingest.get("public_post_count")
    share = ingest.get("ingested_share")
    if total_known and share is not None:
        emphasis = "**" if share < 0.5 else ""
        caveats.append(
            f"- {emphasis}Ingested {ingest.get('unique', len(docs)):,} of "
            f"{total_known:,} public posts ({share:.1%}){emphasis}"
        )
    if ingest.get("hiatus_probes"):
        productive = ingest.get("hiatus_probes_productive", 0)
        caveats.append(
            f"- {ingest['hiatus_probes']} wide probe(s) ran after hitting the "
            f"empty-window tolerance; {productive} found further history and "
            f"resumed the walk."
        )
    if ingest.get("stop_reason"):
        caveats.append(f"- Ingestion stopped because: {ingest['stop_reason']}")
    # Phase 2.6: a skipped document is a hole in the corpus, so it belongs in
    # the caveat block rather than only in signals.json.
    dropped = int(ingest.get("timestamp_errors", 0) or 0)
    dropped += int(run_meta.get("hydration", {}).get("timestamp_errors", 0) or 0)
    if dropped:
        samples = ingest.get("timestamp_error_samples", [])[:3]
        caveats.append(
            f"- **{dropped} document(s) were skipped because their timestamp could "
            "not be parsed.** They are missing from the corpus and from every "
            "count below." + (f" Examples: {'; '.join(samples)}" if samples else "")
        )
    hyd = run_meta.get("hydration", {})
    if hyd.get("context_unavailable"):
        caveats.append(
            f"- {hyd['context_unavailable']} reply parents / quote targets were deleted "
            "or unavailable; those documents carry `[unavailable]` context."
        )
    if run_meta.get("budget_stopped"):
        caveats.append("- **Budget was exhausted; these results are partial.**")
    if run_meta.get("synthesis_error"):
        caveats.append(f"- Synthesis problem: {run_meta['synthesis_error']}")
    dropped_findings = run_meta.get("dropped_findings", [])
    for note in dropped_findings[:5]:
        caveats.append(f"- Dropped in enforcement: {note}")
    if len(dropped_findings) > 5:
        caveats.append(
            f"- …and {len(dropped_findings) - 5} further finding(s) dropped; see run metadata."
        )
    if run_meta.get("structured_output") is False:
        caveats.append(
            "- The provider refused the output schema as too large for "
            "constrained decoding, so the model was asked for JSON in the "
            "prompt instead and the result was validated in Python. Every "
            "guarantee below still holds; the first attempt is just likelier "
            "to need a retry."
        )
    corrected = run_meta.get("corrected_counts", [])
    if corrected:
        caveats.append(
            f"- {len(corrected)} model-stated count(s) were overwritten with the "
            "values computed in Python; signals.json is authoritative."
        )

    # ---- part one: the verdict page ---------------------------------------
    # One page a reader can stop after, every claim carrying its count.
    # Part two below it is the evidence: everything the report has always
    # contained, unchanged, with the coverage block at its top.
    if synthesis is not None:
        out.extend(
            _part_one(
                synthesis=synthesis,
                docs=docs,
                signals=signals,
                run_meta=run_meta,
                tier=tier,
                unknown=unknown,
                confidence_display=confidence_display,
            )
        )

    out.append(_callout(caveats))
    out.append("")

    if synthesis is None:
        out.append("## No synthesis")
        out.append("")
        out.append(
            "The corpus and signals were written, but synthesis did not complete. "
            "Re-run `corpus resynth <dir>` once the cause below is addressed."
        )
        out.append("")
        out.append(f"`{run_meta.get('synthesis_error', 'unknown error')}`")
        out.append("")
        out.append("---")
        out.append("")
        out.append("## Spend")
        out.append("")
        out.append("```")
        out.extend(budget_lines)
        out.append("```")
        return "\n".join(out)

    # ---- summary ---------------------------------------------------------
    out.append("## Summary")
    out.append("")
    out.append(synthesis.summary)
    out.append("")

    if tier is not None and tier.suppresses_inference:
        out.append("## This corpus is too small for inference")
        out.append("")
        out.append(
            f"Only **{tier.document_count} documents** survived filtering, under the "
            f"{THIN_BELOW}-document floor. At that size there is no way to tell a "
            "position someone holds from a thing they happened to say once, so "
            "everything inferential has been switched off rather than guessed at:"
        )
        out.append("")
        out.append("- **Inferred positions** on every axis are suppressed. Only `stated` is shown.")
        out.append("- **Beliefs are listed without structure.** Each one is sourced, but which")
        out.append("  are load-bearing and what each generates is not assessed.")
        out.append("- **Blind spots** are empty — a blind spot is a pattern, and there is no")
        out.append("  run of behaviour here to establish one.")
        out.append("- **What moved** is empty — a change of view needs enough before and after")
        out.append("  to tell them apart.")
        out.append("- **Confidence** is set to `low` in code, not assessed by the model.")
        out.append("")
        out.append(
            "This is a limit of the corpus, not of the subject. What is below is still "
            "sourced and still true; there is simply less of it."
        )
        out.append("")
        out.append(f"**To get the full analysis:** {THIN_REMEDY}")
        out.append("")

    # ---- core model, the centrepiece -------------------------------------
    # Bound to the tier itself rather than a bare bool so the branch below can
    # quote its document count without a second None check.
    flat = tier if (tier is not None and not tier.allow_belief_structure) else None
    structured = flat is None
    out.append("## The generating model" if structured else "## Beliefs, without the structure")
    out.append("")
    if not synthesis.core_model:
        out.append("_No belief survived sourcing. The corpus is too thin to reconstruct a model._")
        out.append("")
    elif flat is None:
        out.append(
            "The beliefs below are ordered by how much else hangs off them. "
            "`generates` is what follows if the belief is held."
        )
        out.append("")
    else:
        # Without this the reader sees a flat list and reads it as a considered
        # tree that happens to have no branches — a stronger claim than the
        # corpus supports, made by omission.
        out.append(
            "**This is a list, not a model.** Each belief below is traced to real posts "
            "and stands on its own. What is missing is the structure: which beliefs are "
            "load-bearing, which follow from others, and what each one generates. Placing "
            f"a belief relative to the others is an inference, and {flat.document_count} "
            "documents cannot support it — so the order here carries no meaning and the "
            "`generates` lists are empty rather than guessed."
        )
        out.append("")
    order = {"load_bearing": 0, "derived": 1, "held_lightly": 2}
    for belief in sorted(synthesis.core_model, key=lambda b: order.get(b.role, 3)):
        out.append(f"### {belief.belief}")
        out.append("")
        cites = _cite(belief.evidence_ids, links)
        # Without structure there is no role to print: "position in the model
        # not assessed" under every single belief would be noise, and the
        # section header and note above already said it once.
        out.append(
            f"_{ROLE_LABELS.get(belief.role, belief.role)}_ · {cites}" if structured else cites
        )
        out.append("")
        for downstream in belief.generates:
            out.append(f"- → {downstream}")
        if belief.generates:
            out.append("")

    # ---- reasoning -------------------------------------------------------
    reasoning = synthesis.reasoning
    out.append("## How they reason")
    out.append("")
    if reasoning.moves:
        out.append("**Moves they make**")
        out.append("")
        for move in reasoning.moves:
            out.append(f"- {move.move} — {_cite([move.example_id], links)}")
        out.append("")
    for label, value in (
        ("What counts as evidence", reasoning.what_counts_as_evidence),
        ("Under disagreement", reasoning.under_disagreement),
        ("What makes them update", reasoning.updates_when),
    ):
        if value:
            out.append(f"**{label}.** {value}")
            out.append("")
    if tier is not None and not tier.allow_blind_spots:
        out.append(
            f"_Blind spots not assessed: {tier.document_count} documents cannot establish "
            "a pattern someone does not see in themselves._"
        )
        out.append("")
    elif reasoning.blind_spots:
        out.append("**Blind spots** _(inferred — the basis is the evidence)_")
        out.append("")
        for spot in reasoning.blind_spots:
            out.append(f"- **{spot.pattern}**")
            out.append(f"  - Basis: {spot.basis}")
            out.append(f"  - Evidence: {_cite(spot.evidence_ids, links)}")
        out.append("")

    # ---- axes, in full ----------------------------------------------------
    # Part one carries the capsules; this is where the chains live.
    out.append("## The axes in full")
    out.append("")
    out.append(
        "Every requested axis appears here. `no signal` means the corpus contains "
        "nothing bearing on it — that is a finding, not a gap in the analysis."
    )
    out.append("")
    silent = [a for a in synthesis.axes if a.signal == "none"]
    for axis in synthesis.axes:
        if axis.signal == "none":
            continue
        out.append(f"### {axis.axis.replace('_', ' ')} — {axis.signal} signal")
        out.append("")
        if axis.stated:
            out.append(f"**Stated.** {axis.stated}")
            out.append("")
        if axis.inferred:
            out.append(f"**Inferred** _({axis.confidence} confidence)_**.** {axis.inferred}")
            out.append("")
            out.append(f"_Chain:_ {axis.reasoning}")
            out.append("")
        out.append(f"Evidence: {_cite(axis.evidence_ids, links)}")
        out.append("")
    if silent:
        out.append(
            "**No signal:** "
            + ", ".join(a.axis.replace("_", " ") for a in silent)
            + ". Nothing in this corpus locates them on "
            + ("these axes." if len(silent) > 1 else "this axis.")
        )
        out.append("")

    # ---- evolution -------------------------------------------------------
    out.append("## What moved")
    out.append("")
    if dates_unreliable:
        # Before/after claims are exactly the output fabricated dates corrupt:
        # the simonw-nox run dated a Later three weeks before its Earlier.
        # Suppression is stated, never silent — an absent section reads as
        # "nothing moved", which is a different claim.
        out.append(
            f"_Suppressed: {unknown} of {len(docs)} documents ({unknown_share:.0%}) "
            "carry no reliable publication date, so earlier and later cannot be told "
            "apart. Evolution analysis on unreliable chronology is worse than no "
            "evolution analysis._"
        )
        out.append("")
    elif not synthesis.evolution:
        if tier is not None and not tier.allow_evolution:
            out.append(
                f"_Not assessed: {tier.document_count} documents cannot separate a before "
                "from an after. This is the corpus size, not a finding about the subject._"
            )
        else:
            out.append("_No view changed inside this corpus. Not manufactured._")
        out.append("")
    if not dates_unreliable:
        for evo in synthesis.evolution:
            out.append(f"### {evo.topic}")
            out.append("")
            out.append(f"- **Earlier:** {evo.earlier}")
            out.append(f"- **Later:** {evo.later}")
            out.append(f"- **Inflection:** {evo.inflection or 'unclear'}")
            out.append(f"- Evidence: {_cite(evo.evidence_ids, links)}")
            out.append("")

    # ---- open questions --------------------------------------------------
    out.append("## Unresolved")
    out.append("")
    if synthesis.open_questions:
        for question in synthesis.open_questions:
            out.append(
                f"- {question.question} _(returned to {question.returned_to}×)_ — "
                f"{_cite(question.evidence_ids, links)}"
            )
    else:
        out.append("_Nothing they return to without settling._")
    out.append("")

    # ---- misreadings -----------------------------------------------------
    out.append("## How to misread this")
    out.append("")
    if synthesis.misreadings:
        for entry in synthesis.misreadings:
            out.append(f"- **{entry.misreading}**")
            out.append(f"  - Why that is wrong: {entry.why_wrong}")
            out.append(f"  - Evidence: {_cite(entry.evidence_ids, links)}")
    else:
        out.append("_Nothing flagged._")
    out.append("")

    # ---- computed signals appendix ---------------------------------------
    out.append("---")
    out.append("")
    out.append("## Computed signals (Python, not the model)")
    out.append("")
    out.append(
        "_Inputs to the analysis above, not findings in themselves. "
        "Every number here is arithmetic done in code._"
    )
    out.append("")
    cadence = signals.get("cadence", {})
    if cadence.get("omitted"):
        # Not a gap: a stated refusal. "2.82 posts/month, 0 median" over a
        # pile of scraped pages measured the fetcher, not the subject.
        out.append(f"- Cadence: not computed — {cadence['omitted']}")
    elif cadence:
        out.append(
            f"- Cadence (X timeline): {cadence.get('mean_posts_per_month', 0)} "
            f"posts/month mean, {cadence.get('median_posts_per_month', 0)} median "
            f"across {cadence.get('active_months', 0)} active months "
            f"({cadence.get('silent_months', 0)} silent)"
        )
    if cadence.get("hiatuses"):
        top = cadence["hiatuses"][0]
        out.append(
            f"- Longest hiatus: {top['days']} days ({top['from']} → {top['to']}); "
            f"{len(cadence['hiatuses'])} gaps of 14+ days total"
        )
    kind = signals.get("kind_mix", {})
    by_source = kind.get("by_source") or {}
    if by_source:
        # Per source, because the kinds mean different things per platform:
        # a GitHub "reply" is a review comment, not a timeline conversation.
        parts = []
        for source in sorted(by_source):
            shares = by_source[source].get("shares", {})
            inner = ", ".join(
                f"{k} {v:.0%}" for k, v in sorted(shares.items(), key=lambda x: -x[1])
            )
            parts.append(f"{source}: {inner}")
        out.append("- Kind mix, per source — " + "; ".join(parts))
    elif kind.get("shares"):
        # signals.json written before the per-source split existed.
        out.append(
            "- Kind mix: "
            + ", ".join(
                f"{k} {v:.0%}" for k, v in sorted(kind["shares"].items(), key=lambda x: -x[1])
            )
        )
    graph = signals.get("conversation_graph", [])[:6]
    if graph:
        out.append(
            "- Most-replied handles: "
            + ", ".join(f"@{g['handle']} ({g['exchange_count']})" for g in graph)
        )
    register = signals.get("register_split", {})
    reg_sources = register.get("by_source") or {}
    if len(reg_sources) > 1:
        # The cross-platform tell: 12-word Bluesky posts and 90-word HN
        # arguments are the same person in two registers, and the difference
        # is itself a finding. Only rendered with 2+ sources — one number is
        # not a comparison.
        parts = [
            f"{source}: ~{reg_sources[source].get('mean_word_count', 0):.0f} words/document"
            for source in sorted(reg_sources)
        ]
        out.append("- Register, per source — " + "; ".join(parts))
    domains = signals.get("outbound_domains", [])[:6]
    if domains:
        out.append(
            "- Most-linked domains: "
            + ", ".join(f"{d['domain']} ({d['share_count']})" for d in domains)
        )
    drift = signals.get("vocabulary_drift", [])
    if drift:
        out.append("- Vocabulary drift (TF-IDF against their own corpus):")
        for bucket in drift:
            terms = ", ".join(t["term"] for t in bucket["terms"][:8])
            out.append(f"  - **{bucket['bucket']}**: {terms}")
    out.append("")

    # ---- spend -----------------------------------------------------------
    out.append("---")
    out.append("")
    out.append("## Spend")
    out.append("")
    out.append("```")
    out.extend(budget_lines)
    out.append("```")
    out.append("")
    return "\n".join(out)


def render_empty_report(
    *,
    subject: str,
    sources: list[str],
    results_seen: int,
    held: int,
    anchors: list[str],
    budget_lines: list[str],
) -> str:
    """The report for a run that completed cleanly and found nothing.

    An empty corpus is a finding, not a crash — and "we found nothing" is a
    different finding from "we found things and could not confirm they were
    theirs". This report exists so a reader can tell those apart without
    opening a log, and so the run does not get re-run repeatedly at cost by
    someone reading emptiness as breakage.
    """
    out = [
        f"# {subject}",
        "",
        "**No public writing could be attributed to this person.** Every source",
        "completed without error and returned nothing attributable, so the corpus",
        "is empty. This is the run's finding, not a failure — re-running will not",
        "change it unless the identity card changes.",
        "",
        "## What was tried",
        "",
    ]
    for line in sources or ["- nothing: no source was configured"]:
        out.append(f"- {line}")
    out.append("")

    if held:
        out.append("## Found, but not confirmed")
        out.append("")
        out.append(
            f"Search saw {results_seen} result(s) and held {held} candidate(s): "
            "pages that matched the name but could not be confirmed as this "
            'person\'s writing. "Nothing could be attributed" therefore means '
            '*found, but unconfirmed* — not "no trace of this person exists".'
        )
        out.append("")
        out.append(
            "Review `unconfirmed.md` in this directory: tick the candidates that "
            "really are theirs and re-run with `--accept-unconfirmed` to build "
            "the corpus from a human decision."
        )
    else:
        out.append("## Nothing was found")
        out.append("")
        out.append(
            f"Search saw {results_seen} result(s) and held none of them, so there "
            "is no candidate waiting on a human decision."
        )
    out.append("")

    if len(anchors) <= 1:
        carried = f"one anchor (`{anchors[0]}`)" if anchors else "no anchors"
        out.append("## What would change the outcome")
        out.append("")
        out.append(
            f"The identity card carried {carried}. Adding a GitHub username "
            "(`--github`) or a personal site (`--site`) gives discovery a place "
            "to start and search an identity to corroborate against — that, not "
            "re-running, is what would change this outcome."
        )
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Spend")
    out.append("")
    out.append("```")
    out.extend(budget_lines)
    out.append("```")
    out.append("")
    return "\n".join(out)
