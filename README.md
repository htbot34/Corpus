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
corpus run --x paulg --resume out/paulg/2026-07-31   # pick up where a dead run stopped
corpus resynth out/paulg/2026-07-31     # re-run synthesis on cached corpus, no fetch
corpus cache stats
corpus cache clear --keep-permanent
corpus cache vacuum
corpus budget log
corpus budget accuracy                  # how wrong --dry-run has been, historically
```

### Options that matter

| Flag | Default | Notes |
| --- | --- | --- |
| `--max-posts` | 3000 | Ingestion stop condition. |
| `--since YYYY-MM-DD` | unset | History floor. |
| `--budget` | 10.00 | Hard stop in dollars, enforced **before** each call. Partial results are always preserved. |
| `--budget-mode` | strict | `strict` refuses any call it cannot fully reserve. `advisory` reserves and reports but never blocks. |
| `--window-days` | 30 | Size of the sliding ingestion window. |
| `--empty-window-tolerance` | 8 | Consecutive empty windows before stopping. See the sharp edge below. |
| `--hiatus-probe` | on | On hitting the tolerance, sweep 12 months in one call before concluding history has ended. |
| `--max-pages` | 20 | Cursor pages per window, the guard against the duplicate-cursor loop. |
| `--no-replies` | off | Replies are included by default and are usually the better corpus. |
| `--include-reposts` | off | When kept, reposts only ever support amplification claims. |
| `--refresh` / `--offline` | off | Bypass the cache / run from cache only. |
| `--dry-run` | off | Print the estimate and stop before any paid fetch. |
| `--resume PATH` | unset | Continue a previous run from its `run.json`. |
| `--map-effort` / `--reduce-effort` | medium / high | Map is extraction, not deep reasoning, so it runs a notch lower. |
| `--log-format` | text | `text` or `json` (one object per line). |
| `--verbose` / `--quiet` | off | Add phase and elapsed time / warnings and errors only. |
| `--capture-raw DIR` | unset | Dump every raw provider response verbatim, before normalization. |

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

### The budget is enforced before the call, not after

`--budget` is a ceiling, not a tripwire. Every billable call is *reserved* first, and a
call that cannot be fully covered is refused rather than made and regretted. Reservations
are held for the duration of a call, so the four concurrent map slices cannot collectively
overshoot.

One consequence worth knowing: the reduce call reserves its **worst case**, which is
`claude-opus-5` emitting all 32,000 output tokens — about **$0.80**. In `strict` mode a
budget below roughly a dollar will therefore refuse synthesis outright, even though the
call would probably have cost a fifth of that. That is the guarantee working: it cannot
promise not to exceed $0.50 when the model *may* spend $0.80. Use `--budget-mode advisory`
if you would rather overshoot than stop.

Reservations are reconciled against actual usage afterwards, and any call where the real
cost exceeded the reservation by more than 20% is reported — an estimator that is wrong
should say so rather than quietly drift. `corpus budget accuracy` shows the history.

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
> a genuinely silent one, a *real* hiatus can still end ingestion early. Two things
> reduce that risk: the default tolerance is **8** (240 days at 30-day windows, not the
> 90 it used to be), and on reaching the tolerance the walk spends one more call
> sweeping the next **12 months** before concluding anything. Find posts and it resumes;
> find nothing and the guess has become evidence. The combination reaches roughly
> **605 days** below the last post seen.
>
> A silence longer than that still truncates, and the report says so in bold, naming
> the last date reached. Raise `--empty-window-tolerance` or `--window-days` for an
> account with known multi-year gaps.

When a window is cut short by either regression, the walk resumes at
`earliest_seen - 1` and deliberately re-covers the unexplored region, accepting some
duplicate reads rather than punching a hole in the history. When a window completes
normally it drops straight to the window floor and re-reads nothing.

---

## A third regression, on the other provider

**The Anthropic output schema is too large for constrained decoding.** Measured against
the live API on 2026-08-02: the reduce schema compiles to a grammar the API refuses with

```
400 invalid_request_error: The compiled grammar is too large, which would cause
performance issues. Simplify your tool schemas.
```

Bisected field by field, 9 of the reduce schema's 13 top-level fields (3,522 bytes) are
accepted and 10 (3,809) are not. The full schema is 4,826. The map schema, at 951 bytes,
is unaffected — which is why the map stage always worked and only the reduce failed.

The reduce now asks for constrained decoding and, if the schema is refused, retries
without it and puts the schema in the prompt instead. That retry is free, because the API
refuses the schema before generating anything, so it does not consume one of the two
billed validation attempts. The real guarantee was never the grammar: every reduce output
is validated against the pydantic model regardless, and a failure retries once with the
error appended. When the fallback fires, the run says so and `report.md` records it.

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
make install     # uv venv + the package + pytest, ruff, mypy
make check       # lint, format, types, secrets, tests — the gate
make coverage    # per-module floors on the money and history paths
```

394 tests, all offline. The suite covers both provider regressions (via
`tests/fake_provider.py`, which can inject repeating cursors, lying empty windows, and
malformed timestamps), thread stitching, context hydration, every signal function, and
the full map-reduce path against a stubbed model client (`tests/fake_anthropic.py`) so
prompts can be iterated on without paying per attempt.

`make check` runs ruff (lint + format), `mypy --strict` over `corpus/`,
`scripts/check_secrets.sh`, and pytest. The GitHub Actions workflow runs exactly the same
commands, with `X_API_KEY` and `ANTHROPIC_API_KEY` deliberately set empty so a test that
reaches for a real key fails loudly rather than passing on a machine that happens to have
one. **Nothing in CI needs a network route or a key.**

Coverage floors are enforced per module rather than on the total
(`scripts/check_coverage.py`): a single total is satisfiable by a well-covered renderer
carrying an untested budget over the line. `budget.py`, `ingest.py`, `client.py`, and
`hydrate.py` each hold at 90%+.

### The two scripts that cost money

Never run by CI; `verify_contract.py` refuses to run when `CI` is set.

```bash
corpus run --x <handle> --max-posts 50 --budget 0.15 --skip-synthesis --capture-raw captures/
python scripts/verify_contract.py            # ~$0.01, monthly drift check
python scripts/verify_contract.py --dry-run  # free, prints the plan
```

> **Fixture provenance.** Mixed, and [`docs/wire-contract.md`](docs/wire-contract.md)
> says exactly which is which.
>
> `user_info.json` matches a **confirmed** live response (2026-07-31): envelope
> `{status, msg, data}`, the real key set, and `createdAt` in the actual ISO-8601
> six-digit-microsecond form (`2010-08-27T20:13:59.000000Z`) rather than the legacy
> `Mon Mar 03 12:00:00 +0000 2014` shape the first draft assumed.
>
> The three **tweet-endpoint** fixtures are still synthetic — written from
> twitterapi.io's documented shapes, not captured from the wire, because the machine this
> was built on has no `X_API_KEY` and no network route to `api.twitterapi.io`. So the
> tests prove the code and the fixtures agree; they do not prove either matches
> twitterapi.io.
>
> `corpus/x/contract.py` states every field name and nesting the code depends on, with a
> severity and a what-breaks note per field. `tests/test_wire_contract.py` checks the
> fixtures against it offline on every run, and `scripts/verify_contract.py` checks the
> same spec against the live API. One spec, two checkers.
>
> The Anthropic side had exactly the same gap, and running it live is what exposed the
> reduce-schema regression above. Stubbed tests prove a pipeline is self-consistent; only
> a real call proves it works.

---

## Known gaps

Honest failure over polish, so these are stated rather than buried:

- **The tweet-object field names are still only probed.** A live ingestion on
  2026-08-02 confirmed `advanced_search` and the `since_time:`/`until_time:` window walk
  work end to end, but `_tweets_from` and `_cursor_from` do not record *which* of their
  candidate names matched, and `normalize_tweet` cannot fail on a renamed `isReply` or
  `quoted_tweet` — it just produces the wrong `kind`, silently. A `--capture-raw` run
  closes this in one pass. `last_tweets` is unexercised entirely.
- **`_cursor_from` falls back to `bool(cursor)`.** If the provider returns a cursor on the
  last page, every window pages to `--max-pages` and pays for it. Correct output, ~20x the
  cost, and nothing would flag it. First item under Pagination in the wire contract.
- **The deleted-or-protected-parent response is unverified.** `hydrate.py` assumes such
  a parent is simply absent from the returned array; if it comes back as an object with an
  error marker, that string is spliced into `context` and read as ground truth by the
  synthesis prompt. (The batch ceiling *was* documented-not-measured, and it was wrong:
  50, not 100. Fixed and pinned.)
- **Estimator accuracy is unproven against real runs.** The machinery to check it exists
  (`corpus budget accuracy`); it has no live data yet.
