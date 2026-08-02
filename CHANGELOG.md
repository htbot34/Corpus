# Changelog

Everything below is the production-readiness pass over the initial one-shot
build. It starts from 6,145 lines and 83 passing tests, and ends at 399 tests
with `make check` green.

Two findings shaped it more than the rest, and both are the same shape: a
pipeline that passed all its tests because the tests never touched the thing
that was wrong.

- **The reduce call had never worked.** The Anthropic API refuses the output
  schema as too large for constrained decoding. Every test passed because the
  stub client returns canned JSON and never submits a schema. Found by running
  it live; see [2026-08-02 — Phase 4](#phase-4--verification).
- **The X fixtures are still synthetic**, because the capture could not run
  here. A live run later confirmed most of the ingestion path anyway — and found
  the batch ceiling was 50, not the documented 100. What remains unverified is
  stated rather than glossed; see [Phase 1](#phase-1--wire-contract) and the
  [2026-08-02 entry](#2026-08-02--live-run-the-batch-ceiling-is-50-not-100).

---

## Phase 0 — Secrets hygiene

**Audit result: clean.** `.env` has never been committed. `git log --all
--full-history -- .env` is empty, `.env` is gitignored and untracked, and a scan
of every blob reachable from every ref found no key material. No rotation
needed.

### Added
- `scripts/check_secrets.sh` — scans tracked plus untracked-not-ignored files
  (everything git would take) for the `sk-ant-` and `new1_` prefixes, for secret
  env vars assigned non-placeholder values, and for generic high-entropy runs
  (32+ chars of key alphabet with mixed case and a digit, which excludes tweet
  ids and git shas). Also re-asserts the `.env` invariants on every run.
- `corpus/redact.py` — `redact()` masks secrets by exact value (every
  secret-shaped env var actually set) and by pattern (key prefixes,
  credential-bearing query params, `X-API-Key`/`Authorization` syntax, Bearer
  tokens).
- `tests/test_secrets.py` — runs the scanner over the tree **and plants a leak
  per pattern**. A scanner that always passes is worse than none.

### Changed
- `ProviderError`, `ScopeViolation`, and `SourceError` now derive from
  `RedactingError`, which redacts **at construction** rather than at `str()`
  time — so `.args` and a bare traceback `repr` are clean too, not just a
  deliberate `str(exc)`.

The exposure closed was not a hardcoded key; there was none. It was
`providers.py` interpolating `resp.text` and httpx exception strings into error
messages, where an upstream 4xx that echoes the request, or a base URL carrying
the key as a query parameter, would land verbatim in a terminal or a pasted bug
report.

---

## Phase 1 — Wire contract

**The live capture could not run.** Two independent blockers, both
environmental: no `X_API_KEY` was present, and the network policy denies
`api.twitterapi.io:443` — the gateway answers `403` to `CONNECT` before TLS,
while control hosts return `200`. A key was supplied later; the network route
was not, and cannot be changed from inside the container.

The tweet endpoints were therefore **unverified** at the end of this phase, and
the repo said so everywhere rather than implying otherwise. A live run on
2026-08-02 later confirmed `advanced_search` and measured the `tweets_by_ids`
ceiling — see the entry at the end of this file. The fixtures themselves are
still synthetic.

### Added
- `--capture-raw DIR` — dumps every raw provider response verbatim, one JSON
  file per call, hooked inside `_get` *before* the status checks, so 4xx bodies
  are captured too. Records request path and query parameter names, response
  headers (`Retry-After` and `x-rate-limit-reset` are what Phase 2.4 needs), and
  a `body_sha256` over the original bytes so verbatimness is provable. Request
  headers are never captured; the body is redacted by exact secret value only,
  never by pattern, so captured post text is never rewritten.
- `corpus/x/contract.py` — every field name and nesting the code depends on,
  stated once, with a severity and a what-breaks note per field. It exists
  because every reader is a fallback chain, which is exactly what turns a
  breaking provider change into a silent one: a renamed field yields zero posts
  and exit 0.
- `tests/test_wire_contract.py` — checks fixtures against the contract, checks
  that `contract.locate_items` and `providers._tweets_from` agree, and asserts
  which endpoints are still unverified so confirming one without updating the
  docs is a red test.
- `scripts/verify_contract.py` — ~$0.01 live drift check against the same spec.
  Probes what fixtures cannot answer: whether `since_time:`/`until_time:` are
  honoured, whether a cursor comes back on the last page, and what a batch does
  with a known-bad id. Refuses to run under CI.
- `docs/wire-contract.md` — what is confirmed, what is assumed, and a checklist
  of exactly what the capture must answer, ordered by cost of being wrong.

### Changed
- `tests/fixtures/user_info.json` rebuilt to the **confirmed** live shape: `msg`
  present in the envelope, the full confirmed key set in live field order, and
  `createdAt` as ISO-8601 with six-digit microseconds — not the legacy
  `Mon Mar 03 12:00:00 +0000 2014` form the first draft assumed.

---

## Phase 2 — The six correctness bugs

### 2.1 The budget is now a ceiling, not a tripwire

`charge()` recorded a cost and *then* raised, by which point the call was done
and the money spent. `would_exceed()` existed and was called from nowhere.

- `Budget.reserve(cost, endpoint)` claims budget **before** the call and raises
  if it cannot be covered. Outstanding reservations count against the limit,
  which is what makes the concurrent case hold — the four in-flight map slices
  each hold their own, so the fourth is refused rather than discovered.
- `settle()` runs *after* `charge()`. Between the two, both the real cost and
  the estimate count, which errs toward stopping early; the other order errs
  toward overshooting, which was the bug.
- Estimators bias high deliberately. Anthropic calls reserve `max_tokens` as
  worst-case output and use the SDK's `count_tokens` when available, falling
  back to 4 chars/token with a 1.25x margin (error bar documented at the
  constant). Anything more than 20% low lands in `Budget.estimate_misses`.
- X page reservations start from the documented page size of 20 and then use
  what the provider actually returns — a fixed worst-case page refused a
  $0.0006 budget's very first call, because a pessimistic reservation is not
  free.
- `--budget-mode strict|advisory`, strict by default.

**Behaviour change:** a budget too small to cover one worst-case call now
refuses cleanly with $0 spent instead of fetching one page. The refusal names
the shortfall and points at `--budget-mode advisory`. In particular the reduce
call reserves ~$0.80, so budgets under about a dollar refuse synthesis in strict
mode.

### 2.2 Handle validation

`ingest.py` interpolated the handle straight into a search query after only
`.lstrip("@")`. A handle with whitespace or a search operator did not fail — it
changed what the query meant, and the run reported "0 posts" as though the
account were empty.

- `corpus/x/validate.py` validates against X's own rule, `^[A-Za-z0-9_]{1,15}$`,
  which happens to exclude every character that could alter query semantics. A
  whitelist, not a blacklist of operators.
- Checked at the CLI boundary, where the error can name the input, and again in
  `ingest_timeline`/`ingest_recent`, which are public functions.
- `--since` now gets a real error instead of a raw `strptime` traceback.

### 2.3 Hiatus truncation

30-day windows at tolerance 3 gave up after 90 days of silence and discarded
everything older — wrong for founders and writers, who go quiet for a quarter
routinely.

- Default tolerance raised to **8**.
- Better than a bigger number: on reaching the tolerance, sweep the next
  **12 months** in one call. Find posts and the walk resumes from just below the
  newest one found; find nothing and the guess has become evidence.
  `--hiatus-probe/--no-hiatus-probe`.
- `IngestStats.probe_confirmed_end` distinguishes "we gave up" from "we
  checked". A walk that successfully crosses a gap still ends on the tolerance
  rule eventually — at the true end of history — and reporting that as "HISTORY
  MAY BE INCOMPLETE" would be crying wolf and would devalue the warning in the
  case that matters.
- `statusesCount` is threaded through as `public_post_count`, so the run and the
  report state "400 of 53,901 (0.7%)", bolded under 50%.

### 2.4 Retry

`time.sleep(2**attempt)` on any 429, 5xx, or transport error.

- Honours `Retry-After` (seconds or HTTP-date) and `x-rate-limit-reset` /
  `x-ratelimit-reset` / `ratelimit-reset` (delta or absolute unix time).
- Falls back to exponential backoff with **full jitter** — undithered backoff
  makes concurrent retries fire in unison, reconvening the herd that caused the
  rate limit.
- Five attempts, no single sleep over 60s even when the server asks for an hour.
  Every retry logs attempt number, reason, and delay.
- Fails fast on what will not change: a 429 whose body indicates a spent balance,
  and `ProxyError`/`UnsupportedProtocol`/DNS-failure `ConnectError`. Not
  hypothetical — it is exactly what this environment produces against
  `api.twitterapi.io`, where a 403-to-CONNECT was retried four times with 7s of
  sleep before reporting a policy denial no amount of waiting would fix.

### 2.5 The anthropic SDK floor

`anthropic>=0.40` while the code calls `output_config` with `format` and
`effort`. Verified empirically rather than assumed: with `anthropic==0.40.0`
installed, `output_config` does not exist and the call raises
`TypeError: got an unexpected keyword argument 'output_config'`.

- Floor raised to `>=0.120.2` — the version actually verified working, not the
  oldest that might be.
- `tests/test_sdk_contract.py` introspects the installed SDK so this cannot
  silently rot again. Confirmed the guard works by installing 0.40.0: five of
  ten tests fail, naming exactly what is missing.
- `uv.lock` generated (30 packages) so "works on my machine because pip picked
  latest" stops being load-bearing.

### 2.6 Silent timestamp fallback

`parse_created_at` returned `datetime.now()` when every parse failed — a silent
data-corruption path in the function that drives pagination. Timestamps feed
`window_earliest`, and the next window ends at `window_earliest - 1`, so
unparseable timestamps made the walk advance **one second per window** while
paying full price for every page.

- Raises `TimestampParseError`. Caught at the ingestion boundary: skip the
  document, count it, keep the first five values as samples, continue.
  `hydrate()` does the same, because it also runs over cached corpora and
  `--offline` input.
- Hydrated *parents* get a fallback instead — a parent's timestamp is never used
  for pagination, cadence, or ordering, so dropping a reply's context because
  its parent had a bad date would trade real quality for no correctness.
- Both confirmed formats pinned by test. Naive timestamps now read as UTC rather
  than local; booleans are rejected rather than treated as epoch 0/1.
- New `IngestCorruption`: a tweet dated later than its own window's `until_ts`
  is impossible, and means either `until_time:` was ignored or timestamps are
  being misparsed. Both corrupt the walk expensively, so it fails loudly.

`ingest.py`'s window-completion logic (the `window_complete` distinction) is
untouched throughout.

---

## Phase 3 — Production hardening

### 3.1 Structured logging
`corpus/logging_setup.py`. Every record carries `run_id` (the same id the spend
table keys on), `phase`, and `elapsed`. `--log-format text|json`, `--verbose`,
`--quiet`. Text stays the default and stays close to the original output —
progress lines already carry their own structure. `--quiet` keeps warnings and
errors: a quiet flag that hides a budget stop is broken, so those were moved off
INFO.

### 3.2 Resumability
`out/{handle}/{date}/run.json`, written **atomically** after every window — the
failures worth resuming from all happen mid-walk, so a manifest written only on
success would never exist when needed. A corrupt or future-version manifest
loads as `None` rather than raising.

Map slice results are checkpointed as they land and reused on resume. Re-paying
a ~40-call map stage to retry a reduce that failed validation is the most
expensive avoidable mistake the tool can make, and reduce is exactly the stage
most likely to need a retry.

`Budget.prior_spend` counts against the limit, so `--budget 10` resumed three
times is a $10 run, not a $30 one.

### 3.3 Cache concurrency
WAL, `busy_timeout=30s`, `synchronous=NORMAL`, and `corpus cache vacuum`.

Found a second bug while doing it: the permanent-row protection was
SELECT-then-INSERT, two statements with a gap. A permanent write landing in that
gap was silently downgraded to expirable by a concurrent TTL'd write — losing
nothing today and re-paying for those tweets next week, which is the worst kind
of bug because nothing looks wrong. Now a single upsert with
`permanent=max(existing, new)`.

### 3.4 Quality gates
ruff (lint + format), `mypy --strict` over `corpus/`, `make check`, and a GitHub
Actions workflow running the same commands. Three rules disabled with reasons at
the config; `B008` in `cli.py` is Typer working as designed.

**mypy found a real bug**: `ingest_recent` referenced `public_post_count`, which
was never a parameter of it — a `NameError` on every call, introduced while
threading `statusesCount` through in 2.3, and 347 tests passed over it because
nothing covered that function.

### 3.5 Coverage
Enforced **per module** rather than on the total, via
`scripts/check_coverage.py`. A single total is satisfiable for the wrong reason:
a well-covered renderer carrying an under-tested budget over the line. Floors of
90% on `budget.py`, `ingest.py`, `client.py`, `hydrate.py`; currently 98.9 /
92.5 / 98.3 / 97.3.

Reaching them meant covering the shallow-pull path (`user_info`, `last_tweets`,
`ingest_recent`, `tweets_by_ids`), which had no tests at all — the same gap that
hid the `NameError`.

### 3.6 Estimate accuracy
Every non-offline run records estimated vs actual cost **and** posts estimated
vs ingested — a cost error and a volume error are different failures. The run
prints the signed error inline. `corpus budget accuracy` reports bias (mean
signed) and spread (mean absolute) separately, because "consistently 30% low"
and "noisy around zero" are different diagnoses, and names the specific
assumptions to look at when the estimator is badly off.

---

## Phase 4 — Verification

`make check` green. 394 tests, all offline.

### The reduce call had never worked

Running the Anthropic half live — the only live verification this environment
permits — returned:

```
400 invalid_request_error: The compiled grammar is too large, which would
cause performance issues. Simplify your tool schemas.
```

Constrained decoding compiles the output schema into a grammar and the API
refuses one over an internal size limit. Bisected live, field by field: 9 of
`REDUCE_SCHEMA`'s 13 top-level fields (3,522 bytes) are accepted, 10 (3,809) are
not. The full schema is 4,826. `MAP_SCHEMA` at 951 bytes is unaffected, which is
why the map stage always worked and only the reduce failed.

Every test passed because `FakeAnthropic` returns canned JSON and never submits
the schema anywhere — the same class of gap as the synthetic X fixtures, on the
other provider.

**Fix:** the reduce asks for constrained decoding and, if the schema is refused,
retries without `format` and puts the schema in the prompt instead. Not shaving
the schema to sit just under a limit we do not control, where the next field
added breaks it again in production after ingestion has been paid for. The real
guarantee was never the grammar: `Synthesis.model_validate_json` already
validates and already retries with the error appended.

The fallback retry does **not** consume a billed attempt — the API refuses the
schema before generating anything, so counting it would have silently halved the
validation retries. The degrade is logged, carried in signals, and stated in
`report.md`.

Verified live end to end: map $0.0185, reduce fell back and succeeded on its
first billed attempt, total **$0.2245**, exit 0, `synthesis.json` written with 5
themes, 12 positions, 2 evolution entries, and 4 hooks.

### Verified
1. `make check` green.
2. Full live run against a real account — **BLOCKED** on the network policy.
3. Tripped budget stops before reduce and still writes `corpus.json` and
   `signals.json` — verified end to end through the CLI.
4. Malformed timestamp: run survives, document skipped, count reported in
   `signals.json` and `report.md` — verified end to end through the CLI.
5. `corpus resynth` costs nothing in X spend — verified with a provider that
   raises if touched at all.
6. `README.md` updated, including corrected defaults, the reservation floor, and
   a Known Gaps section.
7. This file.

**Cumulative live-API spend for the whole task: ~$0.27**, against a $1 ceiling.

---

## 2026-08-02 — Live run: the batch ceiling is 50, not 100

A live `corpus run` reached the end of ingestion and then failed hydration with:

```
400 {"detail":"max 50 tweet_ids per request, please batch into multiple calls"}
```

The documented ceiling was 100 and the code batched at 100 in three places.
Now one constant — `BATCH_LOOKUP_MAX = 50` in `providers.py` — used by the
client-side cap, the chunking in `client.py`, the hydrate docstring and log
line, the fake provider, and the tests. Pinned by `test_the_batch_ceiling_is_fifty`.

**The rest of ingestion worked.** That run exercised `advanced_search`, the
`since_time:`/`until_time:` window walk, `_tweets_from`, `_cursor_from`, and
`normalize_tweet` against real payloads without error. The batch ceiling was the
only mismatch, and it surfaced as a clean 400 *after* `corpus.json` was already
on disk — nothing corrupted, nothing to re-fetch, which is the "paid data
survives" property working.

`advanced_search` is now **CONFIRMED** and `tweets_by_ids` **PARTIALLY
CONFIRMED** in `docs/wire-contract.md` and `corpus/x/contract.py`.
`test_unverified_endpoints_are_declared_unverified` fired exactly as designed
and was updated with the docs, as it is meant to be.

Two things the confirmations deliberately do *not* claim, because "ran without
error" is weak evidence where every reader has a fallback:

- Which of `_tweets_from`'s four candidate array locations actually matched, or
  which cursor field name. Not recorded.
- That `isReply` / `quoted_tweet` / `retweeted_tweet` were read correctly.
  `normalize_tweet` can only raise on a bad timestamp; a renamed classification
  field yields a wrong `kind`, silently.

---

## Still outstanding

- The tweet-object field names are probed, not observed — see the 2026-08-02
  entry above. `last_tweets` is unexercised entirely.
- `_cursor_from`'s `bool(cursor)` fallback would cost ~20x per window if the
  provider returns a cursor on the last page. Correct output, silent cost.
- The deleted-or-protected-parent response is still unverified. (The batch
  ceiling was measured on 2026-08-02: 50.)
- Estimator accuracy has no live data yet.
