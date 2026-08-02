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

_Generated 2026-08-02 22:58 UTC · 16 documents · 2024-01-08 to 2024-08-16_

> **Coverage and caveats**
> 
> - Date range: 2024-01-08 to 2024-08-16
> - Documents analyzed: 15 of 16 in corpus
> - Model-assessed confidence: **medium**
> - Ingestion stopped because: fixture corpus (offline)
> - 1 reply parents / quote targets were deleted or unavailable; those documents carry `[unavailable]` context.
> - Dropped in enforcement: core_model 'invented belief' dropped: no valid evidence ids
> - Dropped in enforcement: reasoning move 'invented move' dropped: example id not in corpus
> - Dropped in enforcement: blind spot 'hand-waved blind spot' dropped: basis is not a chain
> - Dropped in enforcement: axis 'epistemics': inference dropped, reasoning was not a chain
> - Dropped in enforcement: axis 'politics_and_ideology' was not returned; recorded as no signal
> - …and 2 further finding(s) dropped; see run metadata.
> - 1 model-stated count(s) were overwritten with the values computed in Python; signals.json is authoritative.

## Summary

One. Two. Three. Four.

## The generating model

The beliefs below are ordered by how much else hangs off them. `generates` is what follows if the belief is held.

### Process quality is measurable and most people refuse to measure it

_load-bearing_ · [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

- → hostility to interview puzzles
- → preference for work trials

## How they reason

**Moves they make**

- concedes the strongest objection first — [2024-08-16](https://x.com/testsubject/status/1700000000000000170)

**What counts as evidence.** Numbers they gathered themselves.

**Under disagreement.** Grants the point, then narrows it.

**What makes them update.** Someone shows them a measurement they cannot dispute.

**Blind spots** _(inferred — the basis is the evidence)_

- **Treats their own sample as representative**
  - Basis: They twice answer institutional-authority claims with a counterexample from their own hiring data rather than disputing the study, which places the burden of proof on the institution rather than on the dissenter.
  - Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

## Where they land

Every requested axis appears here. `no signal` means the corpus contains nothing bearing on it — that is a finding, not a gap in the analysis.

### institutions and authority — strong signal

**Stated.** Credentials are a lazy proxy for competence.

**Inferred** _(medium confidence)_**.** They treat institutional legitimacy as earned per-claim.

_Chain:_ They twice answer institutional-authority claims with a counterexample from their own hiring data rather than disputing the study, which places the burden of proof on the institution rather than on the dissenter.

Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

### epistemics — weak signal

**Stated.** Prefers measurement to intuition.

Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

**No signal:** politics and ideology, defense intel natsec, technology and ai, economics and markets. Nothing in this corpus locates them on these axes.

## What moved

### work trials

- **Earlier:** two days is enough
- **Later:** two days measures sprinting
- **Inflection:** 2024-06-15
- Evidence: [2024-08-16](https://x.com/testsubject/status/1700000000000000170), [2024-08-14](https://x.com/testsubject/status/1700000000000000165)

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
