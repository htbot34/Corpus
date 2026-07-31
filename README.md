# corpus

Ingests a person's entire public X history, hydrates it into readable context, and
produces a sourced synthesis of what they actually think, how they argue, and how
their views have moved.

X is the primary and usually only source. Substack, RSS, and single web pages are
optional bolt-ons that merge into the same corpus, never the focus.

This is a personal research tool with exactly one user. No web UI, no auth, no
multi-tenancy, no scheduler. It optimizes for signal quality, cost visibility, and
honest failure over polish.

---

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install -e .
cp .env.example .env    # then fill in the two keys
```

### Environment variables

| Variable | Required | What it does |
| --- | --- | --- |
| `X_API_KEY` | yes | twitterapi.io key, sent as the `X-API-Key` header. New keys get ~$1 trial credit, enough for ~6,000 posts. |
| `X_PROVIDER` | no | Provider selector. Defaults to `twitterapi_io`. |
| `X_BASE_URL` | no | Override the provider base URL (proxy, testing). |
| `ANTHROPIC_API_KEY` | yes | Used for the map (`claude-sonnet-5`) and reduce (`claude-opus-5`) passes. |
| `CORPUS_CACHE_DB` | no | SQLite cache path. Defaults to `~/.corpus/cache.db`. |

---

## Usage

```bash
corpus run --x paulg
corpus run --x paulg --max-posts 5000 --since 2020-01-01 --budget 15
corpus run --x someone --dry-run
corpus run --x someone --also-substack example.com
corpus resynth out/paulg/2026-07-31     # re-run synthesis on cached corpus, no fetch
corpus cache stats
corpus cache clear --keep-permanent
corpus budget log
```

### Options that matter

| Flag | Default | Notes |
| --- | --- | --- |
| `--max-posts` | 3000 | Ingestion stop condition. |
| `--since YYYY-MM-DD` | unset | History floor. |
| `--budget` | 10.00 | Hard stop in dollars. Partial results are always preserved. |
| `--window-days` | 30 | Size of the sliding ingestion window. |
| `--empty-window-tolerance` | 3 | Consecutive empty windows before stopping. See the sharp edge below. |
| `--max-pages` | 20 | Cursor pages per window, the guard against the duplicate-cursor loop. |
| `--no-replies` | off | Replies are included by default and are usually the better corpus. |
| `--include-reposts` | off | When kept, reposts only ever support amplification claims. |
| `--refresh` / `--offline` | off | Bypass the cache / run from cache only. |
| `--dry-run` | off | Print the estimate and stop before any paid fetch. |
| `--map-effort` / `--reduce-effort` | medium / high | Map is extraction, not deep reasoning, so it runs a notch lower. |

---

## Why not the official X API

As of February 2026 X moved new developers to pay-per-use at **$0.005 per post read**,
killed the free tier, and gated full-archive search behind Enterprise at roughly
**$42,000/month**. The official `/2/users/:id/tweets` endpoint also caps at ~3,200 posts
and in practice drops `next_token` far earlier on high-volume accounts.

twitterapi.io costs about **$0.15 per 1,000 tweets** and **$0.18 per 1,000 profiles**,
has no 3,200 cap, and reaches back to the early platform years. At 3,000 posts that is
a difference of roughly $0.68 versus $15.00 — before you even reach the archive
paywall.

### Swapping providers

`corpus/x/providers.py` defines the `XProvider` protocol. `twitterapi_io` is fully
implemented; `apidance` and `socialdata` are stubs that raise `NotImplementedError`
naming exactly which endpoints to add. To add one:

1. Implement `user_info`, `last_tweets`, `advanced_search`, `tweets_by_ids`, `close`.
2. Register the class in `PROVIDERS`.
3. Set `X_PROVIDER=<name>`.

Nothing outside that file needs to change. If a provider's JSON shape differs,
`normalize_tweet` in `corpus/x/client.py` is the only other seam.

---

## Cost model

Two independent meters: X data and Anthropic tokens. Both are tracked per call in
`budget.py`, written to the `spend` table, and printed broken out at the end of every run.

**Unit prices** (as configured in `corpus/budget.py`):

| Item | Price |
| --- | --- |
| Tweet read (twitterapi.io) | $0.15 / 1,000 |
| Profile read (twitterapi.io) | $0.18 / 1,000 |
| Minimum charge per request | $0.00015 |
| `claude-sonnet-5` | $2 / $10 per MTok (introductory, through 2026-08-31; $3 / $15 after) |
| `claude-opus-5` | $5 / $25 per MTok |
| Prompt cache write / read | 1.25× / 0.10× the input rate |

Sonnet 5 introductory pricing is date-aware in code, so the printed spend matches the
invoice instead of being conveniently vague.

### Worked examples

Assumes ~50% of posts are replies or quotes needing one extra read to hydrate the
parent, ~120 tokens per document, ~30k-token map chunks, and one reduce call.

| Posts | Tweet reads | X data | Map chunks | Anthropic | **Total** |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 750 | $0.11 | 2 | $0.33 | **~$0.44** |
| 3,000 | 4,500 | $0.68 | 12 | $1.16 | **~$1.83** |
| 10,000 | 15,000 | $2.25 | 40 | $3.47 | **~$5.72** |

Two things move these numbers most: hydration ratio (a reply-heavy account costs more
because every reply needs its parent) and `--reduce-effort` (the reduce call is a small
fraction of tokens but the most expensive per token).

The default `--budget 10.00` comfortably covers a 10,000-post run. `--dry-run` prints
the estimate for a specific target after one profile lookup (~$0.0002).

### Where the money does *not* go twice

- Hydrated parents are cached **permanently** — old tweets do not change.
- Ingested tweets are cached permanently too, so `--offline` and `resynth` are free.
- The reduce prompt's corpus block is cached, so a validation retry is a cache read.
- `corpus cache clear --keep-permanent` clears everything *except* the tweets you paid for.

---

## The two provider regressions this is built around

These are real, documented, and will silently corrupt a naive implementation.

**1. Cursor pagination is unreliable on historical data.** On queries reaching older
tweets — especially 2019 through 2022 — the API sometimes returns the same tweets again
under a different cursor value, so a cursor loop never terminates.

The fix: never paginate deep history by cursor. Slide a fixed time window backwards
using `since_time:`/`until_time:` in unix seconds (the date-level `since:`/`until:`
operators are no longer honoured by the underlying index and return empty or unbounded
results). Cursor-paginate only *within* a window, capped by `--max-pages`. Deduplicate
by tweet id across windows regardless, and alarm if the dedupe rate exceeds 25% — a
high rate means the window logic broke, and it lands in the report as a caveat.

**2. Some time windows return empty despite containing tweets.** An empty window is
therefore not the end of history. The walk continues until
`--empty-window-tolerance` consecutive empties, and every empty window is logged and
recorded in `signals.json` so a systematic gap is visible rather than silent.

> **Sharp edge worth knowing.** Because a lying empty window is indistinguishable from
> a genuinely silent one, a *real* hiatus longer than `window_days × tolerance`
> (90 days at the defaults) will end ingestion early. The run tells you — `stop_reason`
> is recorded and surfaced in the report's caveat block — but for an account with known
> long silences, raise `--empty-window-tolerance` or `--window-days`.

When a window is cut short by either regression, the walk resumes at
`earliest_seen - 1` and deliberately re-covers the unexplored region, accepting some
duplicate reads rather than punching a hole in the history. When a window completes
normally it drops straight to the window floor and re-reads nothing.

---

## Pipeline

```
ingest → hydrate → signals → map → reduce → render
```

**Hydration is the quality lever**, and the reason this is not a profile summarizer:

1. **Thread stitching.** Consecutive self-replies collapse into one `Document` with
   `kind="thread"`, bodies joined, root's URL and timestamp, engagement from the root
   only, `part_count` set. Threads are where people make actual arguments.
2. **Reply parent hydration.** Every reply gets its parent's verbatim text, batched 100
   at a time. A deleted or protected parent becomes `[unavailable]` and the document is
   kept.
3. **Quote target hydration.** Same treatment; the quoted post is the subject of the
   commentary. Inline quotes in the payload cost nothing extra.
4. **Media-only classification.** Under 15 characters of text plus an attachment →
   `kind="media_only"`, excluded from synthesis, counted in cadence.
5. **Repost handling.** Excluded by default.

`context` is the single most important field in the tool. A reply without its parent is
noise; "completely backwards" is meaningless until you know what it answers.

### Signals are computed in Python, never by the model

Models are bad at arithmetic and confident about it. `signals.json` holds cadence
(with bursts and 14+ day hiatuses), kind mix, the conversation graph, outbound domains,
per-kind engagement baselines and outliers, register split, and TF-IDF vocabulary drift
per 6-month bucket against the person's own corpus. It is injected into the reduce
prompt as ground truth.

### The synthesis rules are enforced, not just requested

The system prompt states the rules; `synthesize.py` then makes two of them true in code:

- **Every `evidence_ids` entry is checked against the real corpus.** Findings citing an
  id that does not exist are dropped, and the drops appear in the report.
- **Every count with a counterpart in `signals.json` is overwritten** with the computed
  value. Theme post counts are left alone — those are inherently model-assigned.

Reduce output is validated against a pydantic model. On failure it retries once with
the validation error appended; on a second failure it dumps the raw output to
`reduce_raw_output.txt` and exits non-zero.

---

## Output

Written to `out/{handle}/{YYYY-MM-DD}/`:

| File | Contents |
| --- | --- |
| `report.md` | Themes ranked by corpus share, every claim hyperlinked to its source post, coverage caveats in a callout at the top, spend summary at the bottom. |
| `synthesis.json` | The validated schema, for piping into downstream drafting. |
| `corpus.json` | Every hydrated `Document`. |
| `signals.json` | The computed metrics. |

`corpus.json` and `signals.json` are written **before** synthesis runs, so a synthesis
failure never costs you the data you paid for.

---

## Secondary sources

One file each in `corpus/sources/`, merging into the same corpus.

- `--substack DOMAIN` — paginates `/api/v1/archive`, fetches bodies via
  `/api/v1/posts/{slug}`, falls back to `/feed`. Paywalled posts keep title and
  subtitle only.
- `--rss URL` — any feed: Medium, Ghost, WordPress, personal blogs.
- `--url URL` — a single page, readability-style extraction.

All three are free (plain HTTP, no metered API) and non-fatal: a failure logs and the
run continues on X alone. Adding a platform should mean one new file in `sources/`. If
it requires editing `synthesize.py`, the abstraction is wrong.

---

## Scope boundaries

Public content published under the person's own name only. No private or protected
accounts, no follower graph enumeration, no DMs, no authentication bypass, no paywall
circumvention. An adapter that would require any of that fails with an explanation
instead.

---

## Development

```bash
uv pip install -e . pytest
python -m pytest -q
```

83 tests, all offline. The suite covers both provider regressions (via
`tests/fake_provider.py`, which can inject repeating cursors and lying empty windows),
thread stitching, context hydration, every signal function, and the full map-reduce
path against a stubbed model client (`tests/fake_anthropic.py`) so prompts can be
iterated on without paying per attempt.

> **Fixture provenance.** The fixtures in `tests/fixtures/` are **synthetic** — written
> from twitterapi.io's documented response shapes, not captured from the wire, because
> the machine this was built on had no network route to the provider. Regenerate them
> from real data with `python tests/fixtures/_generate.py` as a starting point, or
> better: run `corpus run --x <handle> --max-posts 50 --budget 0.10 --skip-synthesis`
> and copy the cached raw payloads over them. If the real shapes differ,
> `normalize_tweet` in `corpus/x/client.py` is the only place that needs to change.
