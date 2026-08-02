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

from datetime import datetime, timezone
from typing import Any

from .models import Document, Synthesis
from .synthesize import EVIDENCE_CAP

ROLE_LABELS = {
    "load_bearing": "load-bearing",
    "derived": "derived",
    "held_lightly": "held lightly",
}


def _link_map(docs: list[Document]) -> dict[str, Document]:
    return {d.source_id: d for d in docs}


def _cite(ids: list[str], links: dict[str, Document]) -> str:
    """Render evidence ids as markdown links to the source posts.

    Capped here as well as in `prune_unsourced`, because `--render-only` runs
    against whatever synthesis.json is on disk — including one edited by hand.
    """
    out = []
    for doc_id in ids[:EVIDENCE_CAP]:
        doc = links.get(doc_id)
        if doc is None:
            continue
        out.append(f"[{doc.published_at.strftime('%Y-%m-%d')}]({doc.url})")
    return ", ".join(out) if out else "_no source_"


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

    out.append(f"# @{handle} — how they think")
    out.append("")
    out.append(f"_Generated {generated} · {len(docs)} documents · {signals.get('date_range', '')}_")
    out.append("")

    # ---- coverage caveats, up top where they cannot be missed -------------
    caveats: list[str] = ["**Coverage and caveats**", ""]
    cov = synthesis.coverage if synthesis else None
    if cov:
        caveats.append(f"- Date range: {cov.date_range or signals.get('date_range', 'unknown')}")
        caveats.append(
            f"- Documents analyzed: {cov.total_documents or len(docs)} of {len(docs)} in corpus"
        )
        caveats.append(f"- Model-assessed confidence: **{cov.confidence}**")
        for gap in cov.gaps:
            caveats.append(f"- Gap: {gap}")
    else:
        caveats.append(f"- Date range: {signals.get('date_range', 'unknown')}")
        caveats.append(f"- Documents ingested: {len(docs)}")

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

    # ---- core model, the centrepiece -------------------------------------
    out.append("## The generating model")
    out.append("")
    if not synthesis.core_model:
        out.append("_No belief survived sourcing. The corpus is too thin to reconstruct a model._")
        out.append("")
    else:
        out.append(
            "The beliefs below are ordered by how much else hangs off them. "
            "`generates` is what follows if the belief is held."
        )
        out.append("")
    order = {"load_bearing": 0, "derived": 1, "held_lightly": 2}
    for belief in sorted(synthesis.core_model, key=lambda b: order.get(b.role, 3)):
        out.append(f"### {belief.belief}")
        out.append("")
        out.append(
            f"_{ROLE_LABELS.get(belief.role, belief.role)}_ · "
            f"{_cite(belief.evidence_ids, links)}"
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
    if reasoning.blind_spots:
        out.append("**Blind spots** _(inferred — the basis is the evidence)_")
        out.append("")
        for spot in reasoning.blind_spots:
            out.append(f"- **{spot.pattern}**")
            out.append(f"  - Basis: {spot.basis}")
            out.append(f"  - Evidence: {_cite(spot.evidence_ids, links)}")
        out.append("")

    # ---- axes ------------------------------------------------------------
    out.append("## Where they land")
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
    if not synthesis.evolution:
        out.append("_No view changed inside this corpus. Not manufactured._")
        out.append("")
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
    graph = signals.get("conversation_graph", [])[:6]
    if graph:
        out.append(
            "- Most-replied handles: "
            + ", ".join(f"@{g['handle']} ({g['exchange_count']})" for g in graph)
        )
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
