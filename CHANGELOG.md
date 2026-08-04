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

---

## 2026-08-02 — Cognition-first report

Layered on top of everything above; none of the production-hardening pass was
re-derived or undone. The report used to catalogue what someone posts about. It
now reconstructs how they think: the load-bearing beliefs that generate their
positions, how they reason, and where they land on worldview axes even when
they never say so directly. Every section that did not answer "how does this
mind work" is gone.

### Schema

New `Synthesis`, seven analytic fields plus coverage: `core_model` (the
centrepiece — load-bearing beliefs, each with `generates` naming the surface
positions that follow), `reasoning` (moves, what counts as evidence, behaviour
under disagreement, what triggers an update, blind spots), `axes`, `evolution`,
`open_questions`, `misreadings`, `coverage`.

**Cut:** `performance_gap` (engagement is reach, not cognition), `hooks`
(outreach, wrong purpose), `themes` (topic selection is signal; ten headers with
eight links each is not), `network` and `reading_diet` as tables (the
interpretation belongs inside the analysis), `voice.register` (sentence length
is decoration).

`signals.py` is unchanged and still computes all of it. Those metrics became
inputs to the analysis instead of output sections.

### The grammar limit, from the other direction

Phase 4 found the reduce schema was 4,826 bytes against a live-bisected ceiling
between 3,522 (accepted) and 3,809 (rejected), and fixed it by degrading to
prompt-guided JSON rather than shaving the schema. That call was right: the
limit is the provider's, not ours.

The cognition-first schema is **3,251 bytes** and fits, so constrained decoding
is the normal path again — and a grammar physically cannot emit the markdown
fence that used to cost a full billed generation.

That inverts one test. `test_reduce_schema_size_is_recorded_against_the_measured_limit`
asserted the schema was still *above* the rejection band, and its own docstring
named this as the thing to watch for: "if REDUCE_SCHEMA ever drops below that
band, constrained decoding starts working again and the fallback stops being
exercised — worth noticing, because the fallback is then untested in
production." It is replaced by `tests/test_schema_size.py`, which fails at 3,400.

**The fallback stays, and its tests matter more now, not less.** It is the path
nothing in normal operation exercises, so the three tests covering it are the
only thing keeping it honest. `_strip_code_fences()` was added in front of every
`model_validate_json` for the same reason: a model asked for JSON in a prompt is
exactly the one that wraps it in backticks.

### Two inference tiers, both enforced

`stated` keeps the existing sourcing rule. `inferred` requires a `reasoning`
chain from specific posts to the conclusion, plus a `confidence`. **The
reasoning is the evidence for the inference:** a chain that is missing, too
short, or a restatement of its own conclusion is dropped in `prune_unsourced()`,
exactly as an unsourced claim is — while the stated tier survives on its own
sourcing. Same rule applies to `blind_spots.basis`.

`signal: "none"` is a required, expected output. Every requested axis appears;
one the corpus cannot speak to says so and cites nothing. Content written onto a
no-signal axis is cleared and the clearing is logged; an axis claiming signal
without valid evidence ids is demoted. Evidence is capped at three ids per
claim, in code and again at render time.

### Configurable axes

New `corpus/profiles.yaml` and `--axes a,b,c`. Six defaults:
`politics_and_ideology`, `institutions_and_authority`, `defense_intel_natsec`,
`technology_and_ai`, `economics_and_markets`, `epistemics`. Each carries a
`probe` injected verbatim into the reduce prompt. An unknown axis name is an
error listing the valid ones, checked at the same CLI boundary as the handle —
before anything is spent.

### Cost

The pre-flight estimator now projects **$0.670** at 1,000 posts ($0.225 X data +
$0.445 Anthropic), down from $0.820 on the old model mix.

- **Haiku on map.** `MAP_MODEL` is `claude-haiku-4-5-20251001`; reduce stays on
  `claude-opus-5`. Both configurable via `--map-model` / `--reduce-model`.
- **`effort` is now opt-in by model.** Haiku 4.5 rejects the parameter outright,
  so sending it blindly — as the code did — would have 400'd every map slice the
  moment the map model changed. `supports_effort()` gates it.
- **Low-signal filter** (`corpus/prefilter.py`, pure Python, no API cost). Drops
  bare acknowledgements, link-only posts with no commentary, and short
  standalone fragments with no context. Never filters by subject: that someone
  treats hobby minutiae with the same analytic seriousness as politics is itself
  a cognitive tell. Threads and short-posts-with-context are never dropped. Drop
  count and reasons land in the report's coverage block. `--no-filter` disables.

The estimator's docstring now states what it is not: `estimate_anthropic_cost`
answers "what will this run cost", `Budget.reserve` answers "can this call be
covered right now" and charges the full `max_tokens`. Conflating them is how a
ceiling turns back into a tripwire.

### Free iteration and migration

- `corpus resynth <dir> --render-only` rebuilds `report.md` from an existing
  `synthesis.json` with zero API calls.
- `run` and `resynth` write `run_meta.json`, so `--render-only` reproduces the
  caveat block without re-deriving anything.
- `corpus.json` is unchanged, so `corpus resynth` works on any existing corpus
  directory with no migration. Only `--render-only` can meet an old
  `synthesis.json`, and it names the offending fields and the command that fixes
  it rather than raising.

### Housekeeping

- Test suite 399 → 430. Still all offline.
- `pyyaml` added with a justifying comment; `types-PyYAML` added to the dev group
  because `mypy --strict` reports a missing stub for `import yaml` on a clean
  machine — which is the machine CI runs on.
- `filterwarnings = ["error"]`: the suppression for `voice.register` shadowing a
  pydantic attribute went away with the field.
- `examples/report.example.md` regenerated in the new shape.

### Not changed, deliberately

`ingest.py`'s window-completion logic, for the same reason Phase 2 left it
alone. The reservation machinery, the retry policy, the manifest, the wire
contract, and `BATCH_LOOKUP_MAX = 50` are all untouched — this pass is the
report, not the pipeline underneath it.

---

## 2026-08-02 — Corpus-size tiers

Nothing in the synthesis varied with document count, which is backwards: a thin
corpus is exactly where inference is most likely to confabulate, because the
model was asked the same questions with less to answer them from and no
instruction to stop.

`corpus/tiers.py` classifies the corpus from the **post-filter** document count
and the tier is injected into the reduce prompt as ground truth. The model is
never asked to assess its own evidence base — self-assessment is the thing
under suspicion.

| Tier | Documents | Effect |
| --- | ---: | --- |
| thin | under 40 | `inferred` and `reasoning` emptied on every axis; `blind_spots` and `evolution` forced empty; `coverage.confidence` forced `low`. Beliefs survive with their evidence; `role` and `generates` are cleared. |
| moderate | 40–149 | Inference allowed, but each one must rest on 3 distinct sourced documents rather than 1. |
| rich | 150+ | Unchanged. |

Enforced in `prune_unsourced` and `enforce_signal_counts`, not merely asked for
in the prompt — a thin corpus that returns a confident inference has it deleted.
The prompt states the same rules so the model does not spend a generation
writing what will be discarded.

### Details worth knowing

- **The tier is computed after filtering.** A corpus of 45 that is really 30
  once the acknowledgements are gone is thin. Classifying before the filter
  would call it moderate, which is exactly the case the tiers exist to catch.
  Two tests pin this, including the mirror case where `--no-filter` moves the
  same documents up a tier.
- **`tier=None` means "no size restriction", not "guess one".** Neither
  `len(valid_ids)` (the whole corpus, filtered documents included) nor
  `len(highlights)` (capped at 60) is the post-filter count, so deriving from
  either would silently invent a fact. `synthesize()` always passes the real
  tier and a test pins that the wiring exists, so the fail-open default cannot
  hide a bug.
- **The report distinguishes "nothing found" from "we did not look".** An empty
  `evolution` at rich reads "No view changed inside this corpus. Not
  manufactured."; at thin it reads "Not assessed: 12 documents cannot separate a
  before from an after." Those are different claims and only one of them is true.
- **The confidence label stops lying.** At a tier that forces confidence, the
  caveat reads "Confidence (set in code, not by the model)" rather than
  "Model-assessed confidence" — the model's assessment is precisely what was
  overridden.
- **`--render-only` recovers the tier without run_meta.json**, from
  `coverage.total_documents`, which is the same post-filter count Python
  classified on.

### Surfacing the secondary sources

`--dry-run` now warns when the projected corpus is under the floor and names
`--substack` / `--rss` / `--url` explicitly, along with the free levers
(`--max-posts`, `--since`, `--empty-window-tolerance`). They were wired up and
easy to miss, and the moment they matter most is before a thin run is paid for.
The warning fires on the estimate block, so it also appears ahead of the spend
confirmation on a real run — not only under `--dry-run`.

The threshold is the same constant the tiers use, so the warning cannot
disagree with the behaviour it warns about.

### Fixed along the way

The new CLI tests initially asserted against a profile no test had set: `corpus
run` opens the real `~/.corpus/cache.db`, so the first dry run cached a profile
and every later one read that instead of the fake. They now redirect
`CORPUS_CACHE_DB` to a tmp path. The suite was leaving an entry in the
developer's cache; it no longer does.

Test suite 458 → 498. `REDUCE_SCHEMA` is unchanged at 3,251 bytes — the tier is
a prompt input and a Python rule, not a model-emitted field.

### The cut runs through `core_model`, not around it

A belief traced to real posts is a sourced claim. `role` and `generates`
describe where that belief sits relative to the others, which is an inference
about structure — the same kind of claim `blind_spots` makes, and unsupportable
for the same reason at the same size.

At thin the beliefs and their evidence survive; `role` becomes `"unclassified"`
and `generates` is emptied.

- **Not `held_lightly`.** That asserts they voice the view but do not defend it,
  which is itself a claim about the belief. A thin corpus does not know that
  either, so forcing it would trade one confabulation for another.
- **`"unclassified"` is absent from `REDUCE_SCHEMA`'s enum** while present in the
  pydantic `Literal`. Grammar-constrained decoding therefore cannot produce it:
  the model has three roles in its vocabulary and this fourth one means "code
  cleared this". It costs nothing — the schema is unchanged at 3,251 bytes.
- **On the prompt-guided fallback path the grammar is not enforcing that**, so an
  `"unclassified"` arriving from the model at a tier that allows structure is
  recorded as `derived` and noted. It must not become a way to opt out of a
  decision the corpus is big enough to support.
- **The report renames the section "Beliefs, without the structure"** and leads
  with "This is a list, not a model." A flat list under "The generating model"
  reads as a considered tree that happens to have no branches — a stronger claim
  than the corpus supports, made by omission. The role line and the `generates`
  arrows are dropped rather than printed empty.

Test suite 499 → 506.

---

## 2026-08-03 — Discovery, phase 1: X stops being the assumption

The tool required an X handle and treated X as primary. That assumption was
wrong, and a live test is what settled it: a public account with **706 statuses
and 308 followers returned zero posts** from every endpoint and query shape —
`last_tweets`, `advanced_search` with and without time bounds, `filter:replies`.
The provider has no coverage for low-follower accounts. The same person's blog,
GitHub, and talks were readable the whole time.

New shape: you identify a person, the tool finds their public writing wherever
it lives, and the existing synthesis pipeline runs over the combined corpus.
`--x` is optional and a run with zero X anchors works end to end. Nothing in
the synthesis half changed.

This entry covers steps 1 and 2 of the build order. Phase 2 search,
`sources/github.py`, `unconfirmed.md`, transcripts, paste, and cross-source
tiering are not here — see [The discovery layer is half-built, on
purpose](README.md#the-discovery-layer-is-half-built-on-purpose). Link-following
went first because it costs nothing and may cover most targets, which was worth
finding out before adding a search bill.

### The failure mode everything else serves

Automated search on a name returns other people. A tool that produces confident,
well-formatted reports and silently attributes a stranger's blog post to the
target is worse than no tool, because the output is indistinguishable from a
correct one. Where coverage and attribution conflict, attribution wins.

### The identity card

`corpus/identity.py`, plus `corpus profile` to write it. Name, employer, role,
location, the anchors confirmed to be theirs, and the known false positives.
Targets live in a `profiles.yaml` resolved from `$CORPUS_PROFILES`, then the
working directory, then `~/.corpus` — deliberately not the package's own
`profiles.yaml`, which holds the axes and ships in the wheel.

- **Anchors are validated where they are written down**, not where they are
  used. An anchor reaches a URL through several callers, and each of them
  getting it right independently is how one of them gets it wrong.
- **An unknown anchor kind is an error naming the valid ones**, the same rule
  `--axes` follows: a typo that silently drops an anchor produces a report that
  looks complete and is not.
- **LinkedIn, Facebook, and Instagram are refused at that same boundary**, with
  the reason, rather than three phases later in the middle of a paid run.
- **Saving is read-modify-write.** The file is hand-edited, and an appender
  produces two `targets:` keys, of which YAML keeps one.

### Attribution on every document

`Document` gains `attribution`, `attribution_basis`, and
`attribution_confidence`. Four tiers: `anchor` (you supplied it), `linked`
(reached from an anchor), `corroborated` (search plus two identity signals),
`name_match` (the name and nothing else — never ingested by default).

- **The default is `anchor`**, because every document written before this
  existed came from a handle the user typed. An older `corpus.json` therefore
  loads with the true value rather than a plausible-looking one.
- **`Thread.collapse()` carries it through.** Threads are the best documents in
  an X corpus, and a silent downgrade to the field default would be the worst
  place to lose provenance.
- **`sources.base.attribute()` stamps a batch after the fact** rather than
  threading provenance through `SecondarySource.fetch`. An adapter cannot know
  how its target was arrived at: the same Substack fetch is an anchor when the
  user typed the domain and `linked` when discovery found it in a GitHub bio.
- **`REDUCE_SCHEMA` is unchanged at 3,251 bytes.** Attribution is a property of
  the corpus and the report, not a field the model emits.

### Phase 1: link-following

`corpus/discovery.py`. From each anchor: the X bio and pinned post, the GitHub
`blog` field, bio, and profile README, the Substack about page, and a personal
site's links, declared feed, or probe paths. Everything is cached, so a second
run over the same target is free.

**The scoring turns on one distinction.** "Reached by following a link from an
anchor" is not sufficient: a personal site links to other people's blogs
constantly.

- *Declared* surfaces are fields a person filled in about themselves — a GitHub
  `blog` field, an X bio URL. A link there is a claim of ownership, so it is
  `linked` unconditionally.
- *Page* surfaces are prose — a profile README, an about page, a homepage. A
  link there is `linked` only with a corroborating signal, and otherwise held as
  `name_match`.

Without that split, a profile README — mostly other people's projects, badges,
and papers — would put half of someone's reading list in the corpus.
`test_a_readme_link_to_a_stranger_is_held_not_ingested` is the test that names
it.

Other decisions worth stating:

- **A declared feed beats probing.** A site advertising
  `<link rel="alternate">` costs one request; only a site that does not gets the
  six probe paths. The homepage is then dropped, because a homepage with the
  feed in hand is a nav bar with a photograph.
- **The pinned-post read is the only metered call in the phase**, so it happens
  only when a client is handed in and never under `--dry-run`. Omit it and the
  phase is free outright.
- **A profile link back to an anchor corroborates without becoming a source.**
  `github.com/jsmith` on their own site is evidence, not writing.
- **Discovery is never fatal.** A dead host, a redirect loop, or a hit fetch cap
  degrades the corpus and is reported. It runs before anything has been paid
  for and must not be the reason a run dies.

### Wiring

- `--x` optional; a run with no X anchor never constructs a provider and needs
  no `X_API_KEY`.
- `--no-discover` means "do not follow links", **not** "do not read what I gave
  you". Anchors are read either way; conflating the two would make the flag
  useless.
- `--substack` becomes a card anchor, so its about page is crawled. `--rss` and
  `--url` stay direct source flags: both repeatable, neither crawled, and "read
  this page" is a different statement from "this person owns this domain".
- **Discovery and every non-X source are read before the estimate and before the
  spend prompt**, because both are free and because on a zero-X run an estimate
  that ignored them would be an estimate of nothing.
- The estimate splits four ways — discovery, fetch, map, reduce. A total cannot
  tell you which phase surprised you.
- New `discovery.json` records the card, every candidate, why each was believed,
  and the held `name_match` candidates. A source the run declined to read is
  written down rather than forgotten.
- `report.md` shows the attribution mix in coverage and flags a finding resting
  only on corroborated evidence. It stays **silent** on an all-anchor corpus: a
  line reading "100% certain" on every report teaches the reader to skip the one
  where it matters.
- The report title is the person, not the handle, when there is no X account.

### Found on the way through

`_fetch_one` now converts any adapter failure into a `SourceError`. Adapters
wrapped HTTP status codes but not transport failures — a DNS miss, a TLS error,
a proxy 403 — and those arrived as httpx exceptions that no caller caught,
killing a run that should have degraded. Surfaced by a test whose feed was not
seeded, which is the honest way to find it.

### Housekeeping

- Test suite 506 → 584, still all offline. The discovery tests replace
  `http_client` with something that fails the test if it is ever constructed, so
  the suite cannot quietly acquire a network dependency.
- `estimate_anthropic_split` was factored out of `estimate_anthropic_cost`,
  which now calls it. Same arithmetic, reported per phase.
- No new dependencies.

### Not changed, deliberately

`ingest.py`'s window-completion logic, the reservation machinery, the retry
policy, the manifest, the wire contract, `BATCH_LOOKUP_MAX = 50`, and every
schema the model sees. This pass is about finding the corpus, not about what
happens to it afterwards.

---

## 2026-08-03 — Branch consolidation, and GitHub as a source

### One trunk

Four branches existed, each a strict ancestor of the next, and a fresh session
had already branched from the wrong one and lost every production fix. Verified
ancestry across all four (39 commits, **0 reachable from any branch and not
from the discovery tip**), then created `main` at that tip and pushed it.

Two steps could not be completed from inside the sandbox and are left for a
human: the repository default is still the oldest branch, and branch deletion
is refused. The agent proxy intercepts `api.github.com` and answers
`GitHub access is not enabled for this session` to `/repos/{owner}/{repo}`,
while the git proxy returns 403 to any ref deletion. Neither the MCP GitHub
server nor `gh` exposes a default-branch setter. Until the default moves, a new
session still clones the 1-commit build — which is the exact failure this was
meant to end, so it is stated here rather than buried in a summary.

### `sources/github.py`

For a technical person with no X presence, GitHub is often the only place their
reasoning is written down in public — and the valuable part is the argument in a
review comment, not the repo list. So no repo metadata is read at all.

Two paths, neither requiring a key. `events/public` yields review comments
(keeping the diff hunk as `context`, for the same reason a reply keeps its
parent post), issue and PR comments, and commit messages. `search/issues?q=
commenter:{login}+type:pr` then each thread's `comments_url` reaches further
back, filtered to comments the subject actually wrote. `GITHUB_TOKEN` raises
60/hr to 5,000/hr; without it the thread cap drops from 25 to 8, because 25
reads would be a third of the anonymous hourly budget spent on one person.

### What the capture changed

Three payloads were pulled from the live API with a token on 2026-08-03,
scrubbed into `tests/fixtures/github_*.json`, and the captures deleted.
`tests/fixtures/_scrub_github.py` is the record of exactly what was changed:
key names and nesting are real, identifiers and prose are not.

**No `PushEvent` carries commit messages.** All 69 in the capture had the
payload `{repository_id, push_id, ref, head, before}` — no `commits`, no
`size`, no `distinct_size`, and an undocumented `repository_id`. Consistent
across a 50-push repo and a 19-push hobby repo, so not size truncation. The
extraction and the wip/typo/merge filter are built and tested against the
documented shape, and `push_events_without_commits` counts the gap so a run
reports "4 pushes, 0 with messages attached" instead of looking like an account
that never commits.

**A `search/issues` item is not the subject's writing.** Each item is the pull
request they *commented on*; `item.body` and `item.user` belong to whoever
opened it, and **0 of 20 items in the capture were authored by the subject**.
Reading `item.body` would have attributed twenty strangers' PR descriptions to
the target — the attribution failure the whole discovery layer exists to
prevent, arriving from inside a source rather than from search. `body` is
therefore never read: the search result is a list of pointers.

### The contract, and what it caught

`corpus/sources/github_contract.py` reuses `corpus/x/contract.py`'s primitives
rather than copying them. Three additions to the shared module, each earned:

- `ROOT_ARRAY` (`"$"`) — GitHub's events feed is a bare array with no envelope,
  and a contract that could not say so would have to describe a shape that does
  not exist.
- `Field.nullable` — GitHub returns null constantly for fields that exist and
  are unset. An absent key means a rename; a null means the account has no bio,
  and conflating them makes the checker cry wolf on every profile.
- The array-not-found message no longer names `_tweets_from`, which is not the
  reader on this side.

`tests/test_github_wire_contract.py` cross-checks in both directions, and the
second direction found three real mismatches: `search/issues.user` and
`.created_at` were marked critical/important while the adapter never reads
them, and the events `id` was marked important when dedupe actually keys on the
comment id inside the payload. The contract was wrong, not the code, and now
says so.

### Fixed on the way through

The commit document id was `gh-commit-{sha[:12]}`. A fixture with
zero-padded shas collapsed seven commits into one, which is an artificial case —
but a truncated id turns a collision into a silently dropped commit, so it is
the full sha now.

### Housekeeping

- Test suite 584 → 638, still all offline. The GitHub tests replace
  `http_client` with something that fails the test if it is ever constructed.
- `gh_captures/` deleted and gitignored alongside `captures/`. The blobs remain
  in git history, where they were committed before this session.
- No new dependencies.

---

## 2026-08-03 — Phase 2: search, and proving a candidate belongs

Phase 1 follows links out from the anchors. It costs nothing, and on a target
with a good GitHub bio it may be all you need — which is why it shipped first.
Phase 2 is the other half: finding sources the user does not already know
about.

Almost all of it is machinery for not attributing a stranger's essay to the
subject. A tool that produces confident, well-formatted reports and silently
gets the person wrong is worse than no tool, because the output is
indistinguishable from a correct one. Where coverage and attribution
conflicted, attribution won, every time.

### The scoring model is the part that matters

`corpus/search/scoring.py` is deterministic Python: no model call, no network,
no clock. Same inputs, same verdict, every time, and fully testable for free.
That is not an optimization — it is what makes the decision auditable, and the
decision is whether someone else's writing enters a corpus attributed to the
subject.

The rule the file serves: **absence of confirmation is not the same as presence
of contradiction.** A scorer built only from positive signals accepts the wrong
person cheerfully, because it never finds a reason not to. So negatives are
first-class, checked *before* the positives are counted, and any unresolved one
demotes — a page carrying three strong signals and one contradiction goes to a
human rather than into the corpus.

"Unresolved" does real work there. A page naming a different employer for this
name is a contradiction *unless* the page also names the right one, in which
case it is a page that mentions two companies. Each negative records what would
have resolved it, so the report can say why a page was held rather than only
that it was.

Two decisions worth stating because they are where a threshold of two gets
defeated:

- **Signals that are two views of one fact count once.** A `<meta
  name="author">` tag and a visible "By Jane Smith" are usually the same byline
  rendered twice, and counting both manufactures corroboration out of a single
  claim.
- **Structured author metadata is kept apart from a name in the body.** The
  first is a declaration of authorship by the publisher; the second is as
  likely to be a citation of someone else's work, and on a page *about* a
  person it is certain to be.

`linked` is unreachable from search by construction, and pinned by a test. It
means "reached from a declared field on an anchor", and no quantity of search
evidence turns into a self-declaration.

### A snippet is not evidence

Two passes. *Discover* scores snippets only to decide what is worth fetching
and to throw out what is disqualified by its URL alone. *Verify* fetches the
survivors and scores against the fetched page. Only the second can promote
anything; a candidate never fetched is held however good its snippet looked.

That ordering pays twice: a candidate disqualified on its URL is never fetched,
which saves a request and — in the case of a people-search site — avoids making
one at all.

### The pages that look most relevant are often the least usable

A profile piece, an interview write-up, and a conference bio are all *about*
the target. They rank highly for a name search, they read as relevant to any
name-matching scorer, and they contain the interviewer's prose rather than the
subject's reasoning. They are recorded as context and never enter the corpus.

### Common names stop the phase rather than steering it

Past eight distinct domains matching the name with no other signal, nothing is
ingested, nothing further is fetched, and the run names which card fields would
narrow it. Distinct *domains*, not results: ten pages on one news site is one
publication writing about one person; ten pages on ten domains is ten people.
A tool that guesses on "John Smith" is a liability.

### unconfirmed.md, and the way back in

Everything held becomes an editable checklist with what found it, what matched,
what did not, and what the page says. Ticked entries are ingested as
`corroborated` with basis `user-confirmed` — a person is better evidence than
any signal in the scorer. Unticked entries go into the card's `exclude` list.

That asymmetry makes an unedited file dangerous, since it would reject every
candidate forever, so the file says so at the top and a run given a file with
nothing ticked asks first.

### The provider seam

`corpus/search/providers.py` mirrors `corpus/x/providers.py`, which has
survived one provider change well. `anthropic_search` is implemented against
the server-side `web_search_20250305` tool using the key synthesis already
needs; `exa` and `brave` are stubs naming the exact env var and endpoint to
add.

Two properties of that tool shaped the file. **Billing is per search, not per
call** — `usage.server_tool_use.web_search_requests` at $10/1,000, and an
errored search is not billed — so `max_uses` is pinned to 1, which is what
makes the reservation exact rather than a guess about what the model will
decide to do. And **there is no snippet field**: the readable text comes from
the model's citations, where `cited_text` is a verbatim quotation and is not
billed as tokens.

An errored search is a **200** with an error object where the result list
belongs. A caller that only catches exceptions sees silence; the parser returns
those errors, and the run reports them — otherwise "no web presence" and "the
rate limiter said no" produce the same report.

### Two bugs from the live @paulg run

Both the same shape: the report contradicting itself a few lines apart, with
the honest version in the caveats.

**An axis with one glancing mention had a `weak signal`.** The
`defense_intel_natsec` axis reported weak on one tangential AI-influence-ops
exchange plus a shared link to a .gov visa portal, while the coverage block
said, correctly, that there were no documents on the subject beyond one
glancing exchange. `weak` now requires at least two documents of *substantive
engagement*, and a shared link with no commentary is not engagement — that
judgement already existed in `prefilter.classify`, so it is exposed as
`is_substantive_engagement` rather than reimplemented. The prompt states the
rule; `_enforce_axes` makes it true.

**The header said `94 documents` and the coverage block said `90 of 94`.** The
Python count correction fires on `coverage.total_documents`; the header read
`len(docs)`. It now reads the corrected count.

### The suite was quietly online, and is not any more

Search running by default meant four CLI test files were making real HTTPS
calls to api.anthropic.com and passing anyway, because a failed search is
deliberately non-fatal. Green, "offline", and network-dependent. A conftest
guard now fails any test that builds a live client for search — it caught 16 —
and the CLI fixtures use a provider that answers with nothing and, importantly,
reports no usage, so no spend assertion moved.

### Also

- **Source concentration.** Past 80% from one source the report says so and
  says what that source can speak to, and the same fact is injected into the
  reduce prompt as ground truth. Deliberately *not* enforced in code: "fewer
  than 40 documents" is arithmetic, "a GitHub-only corpus cannot speak to their
  politics" is a judgement about subject matter, and encoding that as a rule
  that deletes axes would be the topical filtering this tool refuses
  everywhere else.
- **The report says what search did** — queries run, results seen, verified,
  ingested, held. A run that searched and found nothing and a run that never
  searched otherwise look identical.
- `--capture-search DIR`, mirroring `--capture-raw`. The search fixture is
  written from documentation and says so in its own `_provenance` field; one
  capture run replaces it with evidence.

### Found by looking at the output

A smoke run over the finished pipeline caught a bug the unit tests could not:
the verification pass refused to fetch a candidate whose *snippet* carried
neither the name nor a signal, and a page that was unmistakably theirs — author
metadata, employer named, linking to their GitHub — was held with "nothing
beyond the name" because its snippet happened to say none of that. The floor
bought no attribution and cost real coverage; the query had already connected
the result to the target, and a fetch is free and capped. Every candidate that
is not rejected outright is now fetched, and the snippet only decides who goes
first when the cap binds.

The report also said those candidates "could not be verified" when most of
them were fetched and scored, and the scoring is what held them. Blaming the
fetch was the wrong story.

### Housekeeping

- Test suite 638 → 772, still all offline.
- No new dependencies.
- `Fetcher`, `kind_for`, and `suffix_match` made public in `discovery.py` so
  Phase 2 reuses one fetch cap rather than growing a second.

---

## 2026-08-03 — The live search check, attempted and blocked

The plan was to run Phase 2 against a real target, replace the
documentation-derived fixture with a real capture, and answer two questions
with data: whether `web_search_20250305` returns the shape the fixture
assumes, and what fraction of real candidates actually reach `corroborated`.

**Neither call could be made.** Two independent blockers, both environmental,
and both verified rather than assumed:

- **No Anthropic credentials.** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
  are unset, there is no `ant` CLI or OAuth profile on the box, and no `.env`.
  `api.anthropic.com` is reachable and answers `401`, so the route is open and
  unauthenticated.
- **Egress denies every candidate host.** The gateway answers `403` to
  `CONNECT` for `simonwillison.net`, `paulgraham.com`,
  `news.ycombinator.com`, and `api.github.com`; the proxy's own status endpoint
  records each denial as `connect_rejected`. Its README is explicit that a 403
  is an organization policy denial and must be reported rather than routed
  around.

The second blocker is the more interesting one, because it would not have
stopped a run — it would have corrupted it. **A search that runs while page
fetches fail produces a misleading answer rather than no answer.** The
verification pass fetches every candidate before scoring it, and a page nobody
can read yields no author metadata, no outbound links, and no employer
mention. Every candidate would have come back held, at 100%, and that number
looks exactly like evidence that the two-signal threshold is too strict. It
would have been an artifact of the network.

So the spend was $0.00, and the effort went into making the run one command
when the blockers lift.

### The search wire contract

`corpus/search/contract.py`, reusing `corpus/x/contract.py`'s primitives for
the third time. It states every field `results_from_message` depends on with a
severity and a what-breaks note, and it is honest that **all of it is
`verified=""`** — assumed from documentation, never observed.

The failure it exists to catch is worse here than on the X side. A zero-result
X ingest is obvious: the report has no documents. A zero-result *search* looks
exactly like a target whose anchors already cover their writing, which is a
normal outcome. If `web_search_tool_result` were renamed tomorrow, every search
would return nothing, every candidate would be held, and the run would exit 0
reporting no web presence with nothing in the output looking wrong.

`tests/test_search_wire_contract.py` checks the fixture against the spec on
every run, in both directions — and the second direction immediately earned
its keep. It caught the contract describing `stop_reason` while no code read
it, which was not a documentation slip but a real gap: a `pause_turn` response
means the server stopped mid-search, and the parser was reading the partial
result as though it were the answer. `results_from_message` now reports it.

One test asserts `WEB_SEARCH.verified` is still empty. It fails the day
someone runs the live checker, which is how they are reminded to record what
they actually saw rather than quietly promoting an assumption.

### The live checker

`scripts/verify_search_contract.py`, the online half, following
`verify_contract.py`'s conventions: refuses under CI, prints a plan and a price
under `--dry-run`, exits 1 on critical drift.

It answers both questions in one command. Every raw response is checked
against the contract field by field. Then a census breaks the outcomes into
corroborated / held / context / rejected, and — for the held ones — counts why:
which negative fired, and **how many sat at exactly one independent signal**.
That last table is the calibration data. If most fetched-and-held candidates
have exactly one signal and are the right person, the bar of two is too strict
for real results, and the script says so along with the more likely fix: a
third weak signal that fires more often, rather than lowering the bar to one.

**It checks page reachability before spending anything**, and refuses to print
a census it knows would be an artifact. Verified working: in this environment
it stops at the probe, explains why the number would mislead, and exits 2
having spent nothing.

### Housekeeping

- Test suite 772 → 792, still all offline.
- `AnthropicSearchProvider.last_raw_message` exposes the untouched response so
  the checker can inspect what arrived without re-parsing a capture file.
- No new dependencies.

---

## 2026-08-03 — The live search run, and the gate that made it meaningless

The run the previous entry was waiting for happened: nine queries,
`$0.18`, nine raw responses captured. It answered the wire-contract question
completely and the calibration question not at all, because the verification
pass never ran. **50 candidates seen, 0 pages fetched.**

The census printed anyway: 23 rejected, 26 held, 1 context, every held one for
"never fetched (page unread)", two signals fired in the whole run. That is
indistinguishable, from the outside, from a scorer whose threshold is too
strict — which is the exact misreading the previous entry said the reachability
probe would prevent.

### What actually stopped it

Not the snippet floor, which was fixed and is intact. Not `--max-fetches`,
which was 20 and never consulted. **The common-name check, at
`verify.py`'s stop-and-ask path, which ran between the two passes and returned
before any page was read.**

The chain, each link confirmed by replaying the captures offline — the replay
reproduces the live census exactly, 23/26/1 and both `employer` signals:

1. **Every snippet was empty.** A `web_search_result` has no snippet field;
   `providers.py` builds one from the model's citations. `SEARCH_SYSTEM` tells
   the model to reply with the single word "done", and a model that writes
   nothing cites nothing. All nine responses: `citations: null`.
2. **So every candidate scored "the name and nothing else"** — `name_present`,
   zero independent signals. That is a fact about the vendor, not the target.
3. **`detect_common_name` counts exactly that shape**, and reached eight
   distinct domains against a threshold of eight. Five of the eight were the
   subject's own academic profiles; one was his own site.
4. **The phase filed all 50 as held and returned.** Zero fetches, by
   construction.
5. **Nothing said so.** The stop appends to `result.notes` and sets
   `common_name`; the script printed neither. The CLI does print both — this
   was the script's blind spot alone.

### The fix: ask the question where the evidence is

A name is common when many *pages* turn out to be about different people, and
only a fetched page can establish that. So `detect_common_name` now counts
fetched scores only, and the refusal happens **after** the verification pass —
demoting what was found rather than skipping it, so the pages that proved the
collision are kept instead of needing a refetch next run.

Asking late costs at most `--max-verify-fetches` plain HTTP requests, which are
free, cached, and already capped for exactly this reason. Asking early cost a
whole phase. The same replay against the fixed code attempts 27 page reads
where it previously attempted none.

`SearchPhaseResult.unread` is the flag that states the condition directly —
candidates scored, no page read — and it lands in `discovery.json` beside
`reads_attempted` rather than being left to be inferred from a zero.

### The script now refuses the census it cannot stand behind

The pre-flight reachability probe was necessary and not sufficient: it answers
"can this machine fetch a page", and the failure was "this run did not read
one". Those are different questions, and only the second makes a census mean
anything.

So the checker prints what the phase did — searches, reads attempted, reads
succeeded, every note, every error — *before* any counting, and then refuses
the census outright when no candidate page was read, or when the phase declined
to promote anything for its own reasons. Exit 2, with the reason. The bucket
counts still print, labelled as what they are: not a calibration table.

### The fixture is real now

`tests/fixtures/web_search_response.json` was rebuilt from one of the nine
captures by `tests/fixtures/_scrub_search.py`, kept as provenance in the same
style as `_scrub_github.py`. Key names, nesting, nulls, token counts and the
`page_age` mix are exactly what arrived; identity is swapped for the suite's
synthetic subject, and platform hosts are deliberately kept because the scorer
matches them by name.

**The contract held on every critical and important field** across nine
responses and 68 results: `content`, `usage`,
`server_tool_use.web_search_requests` (exactly 1 per call), the
`web_search_tool_result` block, `url` and `title` on every result. Four things
the docs did not mention, all additive and none read by the parser: `caller` on
two block types, five extra `usage` keys, `container`/`stop_details`/
`stop_sequence`, and `page_age` occasionally arriving as relative prose
("1 month ago") rather than a date.

The one real mismatch is the absence: **no citations, therefore no snippets,
ever, while the search prompt stands.** `check_search_response` used to report
that as a violation and would have fired on all nine — the normal case is not
drift, and a checker that flags it every run teaches its reader to skip the
line that matters. It is now a stated contract fact, and
`web_search_response_with_citations.json` carries the documented citation shape,
labelled SYNTHETIC, so the code that reads one still has something to run
against. Same status and same reason as `github_events_with_commits.json`.

`WEB_SEARCH.verified` now names the date and the command that produced it, and
the test that guarded its emptiness was inverted rather than deleted: it insists
the claim stays falsifiable.

### `captures/` was committed, and `make check` was red because of it

The previous commit added the nine capture files to the repo. `.gitignore`
lists `captures/`, `check_secrets.sh` names it as a path that is *supposed* to
be ignored, and the scanner found **101 high-entropy strings** in them — the
`encrypted_content` blobs, which look exactly like credential material to any
scanner worth having. The build was red at HEAD before any of this work
started.

They are untracked again (still on disk, still gitignored, still in history at
`231933d` if anyone needs them). The scrubbed fixture is what belongs in the
repo — the rule `gh_captures/` already followed.

### Housekeeping

- Test suite 792 → 808, still all offline. Three of the new tests fail against
  the old gate, which is the only reason to trust them.
- `make check` green again.

---

## 2026-08-03 — Weigh the signals, don't count them

The dustinw run, with pages readable this time, finally produced calibration
data: 50 candidates, 12 pages read, 2 corroborated, 24 held. `links_to_anchor`
— a strong signal — fired six times and promoted twice. The cause was
arithmetic, in `scoring.py`: `count_independent` dropped weak signals and then
counted strong and moderate identically, and `CORROBORATION_THRESHOLD = 2`
counted signals rather than weighing them. Two moderate agreements promoted;
one strong self-declaration did not. The weights existed and decided nothing.

### Corroboration is now points

A strong signal is worth 2 points, a moderate 1, a weak 0.5, and 2.0 points
with no unresolved blocking negative → `corroborated`. One strong
self-declaration is enough; so are two moderate agreements; weak signals
assist without ever promoting on their own.

Nothing else about the model moved:

- `_SIGNAL_GROUPS` deduplication holds, at the strongest member's weight. A
  `<meta name="author">` tag plus a visible byline is one claim worth 2.0
  points — never a strong plus a moderate summing to 3.0.
- The negatives still short-circuit every promotion path, including the new
  single-strong-signal one; the tests pin `about_not_by` and `citation_only`
  against a page that would otherwise promote on a furniture link alone.
- `linked` stays unreachable: confidence is reworked for points but keeps the
  0.8 ceiling under linked's 0.85, and
  `test_search_can_never_promote_to_linked` passes unchanged.
- `CandidateScore.missing` now states points and the per-weight worth, so an
  unconfirmed.md line says what actually happened instead of counting signals
  the scorer no longer counts. `corroboration_points` lands in
  `discovery.json` beside the verdict, and the live checker's calibration
  table is in points too.

### The judgement inside `links_to_anchor`

At a flat strong weight, one anchor link now promotes — which means a
stranger's blog post that mentions the target once and links their GitHub
would be ingested at full corroborated confidence. A page *about* someone
links their profiles too. So the weight is split by placement:

- **Strong** when the page also declares the anchor handle as its own
  (`facts.handles`), or the link sits in the page's own furniture — an author
  or byline block, the header, the footer (`PageFacts.declared_links`, a new
  ~40-line stdlib parser in `pagefacts.py`).
- **Moderate** when it is a bare inline link in body prose.

This is the declared-field-versus-page-prose distinction the attribution
model already treats as load-bearing (a GitHub `blog` field is a
self-declaration; a link in a README body is prose), applied one level down.
It is a judgement, not a measurement: nobody has counted how often real
stranger pages carry furniture links. It errs toward holding, which is the
direction this tool errs everywhere.

Because promotion can rest on it, the furniture detector is deliberately
conservative, and an adversarial review pass earned each bound with a
concrete page that would otherwise have been wrongly ingested:

- `<body>` and `<html>` are never furniture. WordPress stamps
  `author author-<slug>` onto `<body>` on author archive pages, and themes
  add `single-author` sitewide — a marker that swallows the page would turn
  every prose link on half the blogs on the internet into a declaration.
- Class markers match on tokens, not substrings: `post-author` and
  `byline__name` are bylines; `authorization-notice` and
  `authoritative-guide` are not.
- A furniture element must actually close before its links count, so an
  unclosed `<div class="author">` cannot claim the rest of the page, and a
  class-marked block that closes with more than ~400 characters of text
  inside it is a wrapper wearing the class, not a byline.
- One declaration is one fact: when the declared handle is what makes the
  anchor link strong, `declared_handle` does not fire again for the same
  claim — 2.0 points, not 3.0.

### The fetch ceiling stops manufacturing holds

`DEFAULT_MAX_VERIFY_FETCHES` 20 → 40. The dustinw run needed 27 page reads
and the old cap left 14 of its 24 holds unread — a ceiling that stops the
pass from reading pages it has already decided to read measures nothing but
itself. `scripts/verify_search_contract.py` now imports the default instead
of restating it, so the two cannot drift.

And the report's coverage block now says what the fetch story was: the
ceiling, how many holds were never read, and that publishers and aggregators
block roughly half of candidate fetches (12 of 27 pages were readable on the
dustinw run). A reader who sees "24 held" with no fetch story reads it as
"24 rejected", and neither number means that.

### Housekeeping

- Test suite 808 → 816, still all offline.
- No new dependencies.

---

## 2026-08-03 — The test suite was writing to the real spend ledger

Every full test run appended two rows to the developer's actual
`~/.corpus/cache.db` `spend` table: 'map slice 1' at $0.0025 and 'reduce
attempt 1' at $0.0125. Confirmed at exactly two rows per run by pointing
`HOME` at a scratch directory and running the suite.

The offender was `test_resynth_itself_still_works_against_an_old_corpus`,
which drives the real resynth CLI with `tmp_path` for output but never
overrides the cache location — and `default_db_path()` falls through to the
real ledger. The suite-wide guard that exists to keep tests offline blocks
`AnthropicSearchProvider._ensure_client` and nothing else, so a fake-model
run that logs real-looking spend sailed straight past it.

The fix is class-wide rather than instance-wide: an autouse fixture in
`conftest.py` points `CORPUS_CACHE_DB` at a per-test path for every test, so
the next test that forgets gets an empty scratch database instead of the
user's ledger. Tests that redirect the env var deliberately still win —
their monkeypatch runs after the autouse one.

Two tests pin it. A guard test asserts the redirect is live in the test
process and that a pathless `Cache()` — the exact shape of the leak — lands
on it. And the resynth test now asserts its own two spend rows land in the
scratch database, where they are visible, instead of leaking somewhere
nobody looks. Verified by re-running the whole suite under a scratch `HOME`:
817 tests, zero files written under it.

### Housekeeping

- Test suite 816 → 817, still all offline — and now provably so for the
  cache, not just the network.

---

## 2026-08-04 — Footers are where blogrolls live

A live reproduction against HEAD: a stranger's essay on variational inference,
with a footer reading "Friends and people I read" linking the target's
homepage and GitHub, scored `corroborated` at 0.6 confidence and would have
been ingested as the target's own writing. It scored `held` before the
weight change, so this was a regression introduced by it.

The cause was the one exemption left in the furniture detector: `<header>`
and `<footer>` counted as self-identification by tag name, exempt from the
400-character cap on the reasoning that site footers are legitimately big.
They are — and blogrolls, "people I read" and "friends" lists live in exactly
those unbounded footers, on personal blogs, which are the entire population
of pages that link a researcher's homepage.

The fix is a deletion: `header` and `footer` are out of `_FURNITURE_TAGS`.
`<address>` stays, the class/id/itemprop token markers stay with their cap,
`rel="author"` stays, and every guard from the adversarial review stays. The
case the tags bought — a person's own footer linking their own GitHub — is
worth little, because that page also carries a byline or a declared handle;
the case they cost was every blogroll on the web.

Both reproductions are pinned as tests, and `scripts/verify_fix.py` runs them
with no arguments and no network, printing PASS or FAIL in plain English, so
the fix can be confirmed on any machine in one command.

### Housekeeping

- Test suite 823 → 825, still all offline.
- Known intermittent, recorded so it is not forgotten:
  `tests/test_discovery_cli.py::test_discovered_documents_carry_why_they_were_believed`
  fails roughly twice in fifteen full runs and is not reproducible on demand.
  Not investigated here.

---

## 2026-08-04 — A dying X provider degrades the run; it does not end it

A live run on `simonw` crashed with the provider's free-tier QPS limit:

    ProviderError: /twitter/tweet/advanced_search failed after 5 attempts:
    429 ... {"error":"Too Many Requests","message":"For free-tier users, the
    QPS limit is one request every 5 seconds."}

At that moment the run held 215 documents from 36 non-X sources, at zero
cost — a rich-tier corpus — and discarded all of it. `ingest_timeline()` was
called with no exception guard, so the ProviderError walked straight up to
the CLI and aborted the run.

That violates the constraint every other source already honours: adapters
never raise to the CLI — they return ok/partial/failed and the run degrades
with an honest report. X was the one source that could still kill a run.

Now a ProviderError during ingest marks X `failed` (or `partial`, when the
checkpoints had already banked posts — those are recovered from the manifest
ids and the permanent cache rather than re-paid for), records the reason in
`ingest_meta` and the report's coverage block, and the run continues on
whatever the other sources produced. The manifest is deliberately not marked
`ingest_complete`, so a later run resumes the walk from the saved frontier.
Hydration gets the same guard: after a rate-limited ingest, the batch-lookup
call is exactly the next one that would have crashed, and un-hydrated
documents were already worth keeping under a spent budget. If no source
produced anything, the run still exits with "nothing to synthesize" — and
that gate now asks whether the other sources *produced documents*, not
whether discovery merely proposed candidates.

Pinned end to end: a CLI run whose provider raises the live 429 on every
timeline call still synthesizes from RSS documents, reports
`x_status: failed` in signals.json, states the loss in the coverage block,
and leaves the manifest resumable.
