"""report.md rendering.

Themes ranked by corpus share, every claim hyperlinked to its source post,
coverage caveats in a callout at the top, spend summary at the bottom.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import Document, Synthesis


def _link_map(docs: list[Document]) -> dict[str, Document]:
    return {d.source_id: d for d in docs}


def _cite(ids: list[str], links: dict[str, Document]) -> str:
    """Render evidence ids as markdown links to the source posts."""
    out = []
    for doc_id in ids[:8]:
        doc = links.get(doc_id)
        if doc is None:
            continue
        label = doc.published_at.strftime("%Y-%m-%d")
        out.append(f"[{label}]({doc.url})")
    if not out:
        return "_no source_"
    extra = len(ids) - len(out)
    suffix = f" +{extra} more" if extra > 0 else ""
    return ", ".join(out) + suffix


def _callout(lines: list[str]) -> str:
    return "\n".join(f"> {line}" for line in lines)


def render_report(
    *,
    handle: str,
    synthesis: Synthesis | None,
    docs: list[Document],
    signals: dict[str, Any],
    budget_lines: list[str],
    run_meta: dict[str, Any],
) -> str:
    links = _link_map(docs)
    out: list[str] = []
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    out.append(f"# @{handle} — corpus synthesis")
    out.append("")
    out.append(f"_Generated {generated} · {len(docs)} documents · {signals.get('date_range', '')}_")
    out.append("")

    # ---- coverage caveats, up top where they cannot be missed -------------
    caveats: list[str] = ["**Coverage and caveats**", ""]
    cov = synthesis.coverage if synthesis else None
    if cov:
        caveats.append(f"- Date range: {cov.date_range or signals.get('date_range', 'unknown')}")
        caveats.append(f"- Documents synthesized: {cov.total_documents or len(docs)}")
        caveats.append(f"- Kinds included: {', '.join(cov.kinds_included) or 'n/a'}")
        caveats.append(f"- Model-assessed confidence: **{cov.confidence}**")
        for gap in cov.gaps:
            caveats.append(f"- Gap: {gap}")
    else:
        caveats.append(f"- Date range: {signals.get('date_range', 'unknown')}")
        caveats.append(f"- Documents ingested: {len(docs)}")

    ingest = run_meta.get("ingest", {})
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
    for note in run_meta.get("dropped_findings", [])[:5]:
        caveats.append(f"- Dropped as unsourced: {note}")
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

    # ---- themes ----------------------------------------------------------
    out.append("## Themes")
    out.append("")
    themes = sorted(synthesis.themes, key=lambda t: -t.share_of_corpus)
    if not themes:
        out.append("_No themes survived sourcing._")
    for theme in themes:
        flag = " _(thin evidence)_" if theme.low_evidence else ""
        out.append(
            f"### {theme.name} — {theme.share_of_corpus:.1%} of corpus "
            f"({theme.post_count} posts, {theme.trajectory}){flag}"
        )
        out.append("")
        out.append(f"First seen {theme.first_seen or '?'} · last seen {theme.last_seen or '?'}")
        out.append("")
        out.append(f"Evidence: {_cite(theme.evidence_ids, links)}")
        out.append("")

    # ---- positions -------------------------------------------------------
    out.append("## Positions")
    out.append("")
    if not synthesis.positions:
        out.append("_No positions survived sourcing._")
    for pos in synthesis.positions:
        flag = " _(thin evidence)_" if pos.low_evidence else ""
        out.append(f"- **{pos.claim}**{flag}")
        out.append(f"  - Confidence: `{pos.confidence}` · Source: {_cite(pos.evidence_ids, links)}")
        if pos.contradicted_by_ids:
            out.append(f"  - Contradicted by: {_cite(pos.contradicted_by_ids, links)}")
    out.append("")

    # ---- evolution (differentiating field) --------------------------------
    out.append("## Evolution")
    out.append("")
    if not synthesis.evolution:
        out.append("_No view changes found in this corpus. Not manufactured._")
    for evo in synthesis.evolution:
        flag = " _(thin evidence)_" if evo.low_evidence else ""
        out.append(f"### {evo.topic}{flag}")
        out.append("")
        out.append(f"- **Earlier:** {evo.earlier_view}")
        out.append(f"- **Later:** {evo.later_view}")
        out.append(f"- **Inflection:** {evo.inflection_date or 'unclear'}")
        out.append(f"- Evidence: {_cite(evo.evidence_ids, links)}")
        out.append("")

    # ---- performance gap (differentiating field) --------------------------
    out.append("## Performance gap")
    out.append("")
    pg = synthesis.performance_gap
    if not pg.posts_most_about:
        out.append("_Posting subject and traction subject align. No gap to report._")
    else:
        out.append(f"- **Posts most about:** {pg.posts_most_about}")
        out.append(f"- **Gets most traction on:** {pg.gets_most_traction_on}")
        out.append(f"- **Interpretation:** {pg.interpretation}")
        out.append(f"- Evidence: {_cite(pg.evidence_ids, links)}")
    out.append("")

    # ---- argument style --------------------------------------------------
    out.append("## How they argue")
    out.append("")
    for move in synthesis.argument_style.typical_moves:
        out.append(f"- {move}")
    if synthesis.argument_style.how_they_handle_disagreement:
        out.append("")
        out.append(
            f"**Under disagreement:** {synthesis.argument_style.how_they_handle_disagreement}"
        )
    out.append("")
    out.append(f"Evidence: {_cite(synthesis.argument_style.evidence_ids, links)}")
    out.append("")

    # ---- network ---------------------------------------------------------
    out.append("## Network")
    out.append("")
    if synthesis.network:
        out.append("| Handle | Exchanges | Relationship | Evidence |")
        out.append("| --- | ---: | --- | --- |")
        for edge in synthesis.network:
            out.append(
                f"| [@{edge.handle}](https://x.com/{edge.handle}) | {edge.exchange_count} "
                f"| {edge.relationship} | {_cite(edge.evidence_ids, links)} |"
            )
    else:
        out.append("_No conversational network found._")
    out.append("")

    # ---- reading diet ----------------------------------------------------
    out.append("## Reading diet")
    out.append("")
    if synthesis.reading_diet:
        out.append("| Domain | Shares | What it suggests |")
        out.append("| --- | ---: | --- |")
        for entry in synthesis.reading_diet:
            out.append(f"| {entry.domain} | {entry.share_count} | {entry.what_it_suggests} |")
    else:
        out.append("_No outbound links in this corpus._")
    out.append("")

    # ---- open loops ------------------------------------------------------
    out.append("## Open loops")
    out.append("")
    if synthesis.open_loops:
        for loop in synthesis.open_loops:
            out.append(
                f"- {loop.question} _(returned to {loop.returned_to_count}×)_ — "
                f"{_cite(loop.evidence_ids, links)}"
            )
    else:
        out.append("_None found._")
    out.append("")

    # ---- voice -----------------------------------------------------------
    out.append("## Voice")
    out.append("")
    out.append(f"**Register:** {synthesis.voice.register}")
    out.append("")
    if synthesis.voice.hobbyhorses:
        out.append("**Hobbyhorses:** " + ", ".join(synthesis.voice.hobbyhorses))
    if synthesis.voice.tells:
        out.append("")
        out.append("**Tells:** " + ", ".join(synthesis.voice.tells))
    out.append("")

    # ---- hooks -----------------------------------------------------------
    out.append("## Hooks")
    out.append("")
    for hook in synthesis.hooks:
        out.append(f'- **"{hook.opener}"**')
        if hook.anchor_url:
            out.append(f"  - Anchor: {hook.anchor_url}")
        if hook.why_it_works:
            out.append(f"  - Why: {hook.why_it_works}")
    if not synthesis.hooks:
        out.append("_None specific enough to this corpus to be worth keeping._")
    out.append("")

    # ---- avoid -----------------------------------------------------------
    out.append("## Avoid")
    out.append("")
    for avoid_entry in synthesis.avoid:
        out.append(
            f"- **{avoid_entry.topic_or_framing}** — {avoid_entry.reason} "
            f"{_cite(avoid_entry.evidence_ids, links)}"
        )
    if not synthesis.avoid:
        out.append("_Nothing flagged._")
    out.append("")

    # ---- computed signals appendix ---------------------------------------
    out.append("---")
    out.append("")
    out.append("## Computed signals (Python, not the model)")
    out.append("")
    cadence = signals.get("cadence", {})
    out.append(
        f"- Cadence: {cadence.get('mean_posts_per_month', 0)} posts/month mean, "
        f"{cadence.get('median_posts_per_month', 0)} median across "
        f"{cadence.get('active_months', 0)} active months "
        f"({cadence.get('silent_months', 0)} silent)"
    )
    if cadence.get("hiatuses"):
        top = cadence["hiatuses"][0]
        out.append(
            f"- Longest hiatus: {top['days']} days ({top['from']} → {top['to']}); "
            f"{len(cadence['hiatuses'])} gaps of 14+ days total"
        )
    mix = signals.get("kind_mix", {}).get("shares", {})
    if mix:
        out.append(
            "- Kind mix: "
            + ", ".join(f"{k} {v:.0%}" for k, v in sorted(mix.items(), key=lambda x: -x[1]))
        )
    reg = signals.get("register_split", {})
    if reg:
        out.append(
            "- Register: "
            + "; ".join(
                f"{k} {v['mean_word_count']} words/post" for k, v in reg.items() if v.get("n")
            )
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
