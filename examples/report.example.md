<!--
GENERATED EXAMPLE — the synthesis content below came from a STUB model client
(tests/fake_anthropic.py), not from a real Anthropic call. It exists to show the
finished report shape: caveat callout, the generating model, the reasoning
machinery, the two inference tiers held apart, no-signal axes reported rather
than hidden, computed-signals appendix, spend footer.

Everything OUTSIDE the model-authored fields is real: the corpus, the signals,
the hydration stats, the low-signal filter, the id-checking, the inference-chain
enforcement, and the count corrections all ran through the actual pipeline over
tests/fixtures/.

Regenerate a real one with:  corpus run --x <handle>
-->

# @testsubject (STUB MODEL OUTPUT — not real analysis) — how they think

_Generated 2026-08-02 23:47 UTC · 16 documents · 2024-01-08 to 2024-08-16_

> **Coverage and caveats**
> 
> - Date range: 2024-01-08 to 2024-08-16
> - Documents analyzed: 15 of 16 in corpus
> - Confidence (set in code, not by the model): **low**
> - **Corpus tier: thin (15 documents). Inference is switched off** — see the note below the summary.
> - Ingestion stopped because: fixture corpus (offline)
> - 1 reply parents / quote targets were deleted or unavailable; those documents carry `[unavailable]` context.
> - Dropped in enforcement: core_model 'Process quality is measurable and most people refu': structure cleared, thin corpus (15 documents) cannot place a belief
> - Dropped in enforcement: core_model 'invented belief' dropped: no valid evidence ids
> - Dropped in enforcement: reasoning move 'invented move' dropped: example id not in corpus
> - Dropped in enforcement: blind spot 'Treats their own sample as representative' dropped: thin corpus (15 documents) cannot establish a pattern
> - Dropped in enforcement: blind spot 'hand-waved blind spot' dropped: thin corpus (15 documents) cannot establish a pattern
> - …and 6 further finding(s) dropped; see run metadata.
> - 2 model-stated count(s) were overwritten with the values computed in Python; signals.json is authoritative.

## Summary

One. Two. Three. Four.

## This corpus is too small for inference

Only **15 documents** survived filtering, under the 40-document floor. At that size there is no way to tell a position someone holds from a thing they happened to say once, so everything inferential has been switched off rather than guessed at:

- **Inferred positions** on every axis are suppressed. Only `stated` is shown.
- **Beliefs are listed without structure.** Each one is sourced, but which
  are load-bearing and what each generates is not assessed.
- **Blind spots** are empty — a blind spot is a pattern, and there is no
  run of behaviour here to establish one.
- **What moved** is empty — a change of view needs enough before and after
  to tell them apart.
- **Confidence** is set to `low` in code, not assessed by the model.

This is a limit of the corpus, not of the subject. What is below is still sourced and still true; there is simply less of it.

**To get the full analysis:** Raise `--max-posts`, drop `--since`, or widen `--empty-window-tolerance` to reach further back; and merge in their long-form writing with `--substack DOMAIN`, `--rss URL`, or `--url URL`, which cost nothing and land in the same corpus.

## Beliefs, without the structure

**This is a list, not a model.** Each belief below is traced to real posts and stands on its own. What is missing is the structure: which beliefs are load-bearing, which follow from others, and what each one generates. Placing a belief relative to the others is an inference, and 15 documents cannot support it — so the order here carries no meaning and the `generates` lists are empty rather than guessed.

### Process quality is measurable and most people refuse to measure it

[2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

## How they reason

**Moves they make**

- concedes the strongest objection first — [2024-08-16](https://x.com/testsubject/status/1700000000000000170)

**What counts as evidence.** Numbers they gathered themselves.

**Under disagreement.** Grants the point, then narrows it.

**What makes them update.** Someone shows them a measurement they cannot dispute.

_Blind spots not assessed: 15 documents cannot establish a pattern someone does not see in themselves._

## Where they land

Every requested axis appears here. `no signal` means the corpus contains nothing bearing on it — that is a finding, not a gap in the analysis.

### institutions and authority — strong signal

**Stated.** Credentials are a lazy proxy for competence.

Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

### epistemics — weak signal

**Stated.** Prefers measurement to intuition.

Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

**No signal:** politics and ideology, defense intel natsec, technology and ai, economics and markets. Nothing in this corpus locates them on these axes.

## What moved

_Not assessed: 15 documents cannot separate a before from an after. This is the corpus size, not a finding about the subject._

## Unresolved

- How to price work trials fairly _(returned to 2×)_ — [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

## How to misread this

- **That they are anti-credential in general**
  - Why that is wrong: They defend credentials where the measurement is real.
  - Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

---

## Computed signals (Python, not the model)

_Inputs to the analysis above, not findings in themselves. Every number here is arithmetic done in code._

- Cadence: 2.0 posts/month mean, 0.0 median across 3 active months (5 silent)
- Longest hiatus: 160 days (2024-01-31 → 2024-07-09); 2 gaps of 14+ days total
- Kind mix: original 50%, reply 31%, media_only 6%, quote 6%, thread 6%
- Most-replied handles: @criticfriend (2), @ghostaccount (1), @bigcoexec (1), @engmanager (1), @vcpartner (1)
- Most-linked domains: arxiv.org (2), increment.com (1), nature.com (1)
- Vocabulary drift (TF-IDF against their own corpus):
  - **2024-H1**: hiring
  - **2024-H2**: evaluation

---

## Spend

```
X data (twitterapi.io): $0.0024
Anthropic tokens:       $0.0181
--------------------------------------
Total:                  $0.0205 of $10.00 budget
```
