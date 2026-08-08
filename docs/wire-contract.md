# Wire contracts

What this tool assumes about each provider's responses, which of those
assumptions have been **observed on the wire**, and which are still **read from
documentation**. The distinction is the whole point of the document.

Three providers, same machinery:

| | twitterapi.io | GitHub | Anthropic web search |
| --- | --- | --- | --- |
| Machine-readable | `corpus/x/contract.py` | `corpus/sources/github_contract.py` | `corpus/search/contract.py` |
| Checked offline | `tests/test_wire_contract.py` | `tests/test_github_wire_contract.py` | `tests/test_search_wire_contract.py` |
| Checked online | `scripts/verify_contract.py` (~$0.01) | — (see below) | `scripts/verify_search_contract.py` (~$0.13) |
| Fixture provenance | **synthetic**, except `user_info.json` | **captured live**, then scrubbed | **captured live**, then scrubbed |

## Exa search — SYNTHETIC fixtures, defensive reader

`ExaSearchProvider` has **no live captures**: the offline rule means
`tests/fixtures/exa_search_response.json` and
`tests/fixtures/exa_find_similar_response.json` are written to the documented
API shapes and are SYNTHETIC, in the same sense as
`web_search_response_with_citations.json`. These shapes are UNVERIFIED against
the wire, and the first live run is the test.

| Provider | Endpoints called | Key |
| --- | --- | --- |
| `search/providers.py` (`ExaSearchProvider`) | `POST api.exa.ai/search`, `POST api.exa.ai/findSimilar` — body `{query\|url, numResults, contents: {text: true}, excludeDomains}` | `EXA_API_KEY` |

What this means in practice: the field names the provider reads (`results[]`,
`url`, `title`, `publishedDate`, `text`, `costDollars.total`) are
documentation-derived, and a rename would degrade to fewer or thinner results
with errors recorded rather than to wrong attribution — every read is
defensive, the whole hit is preserved in `SearchResult.raw`, and the provider
keeps `last_raw_payload` so a shape surprise is inspectable in the run that
hit it. The pricing constants in `budget.py` (`EXA_COST_PER_SEARCH_REQUEST`,
`EXA_COST_PER_PAGE_TEXT`) are documentation-derived too, could not be re-read
at implementation time (the environment's egress proxy blocks exa.ai), and
carry an operator TODO to verify before the first paid run. `costDollars`,
when the wire supplies a sane value, beats both constants at billing time.
The honest way to promote this from SYNTHETIC to CONFIRMED is a live capture
via `--capture-search` scrubbed the way `_scrub_search.py` records.

## Bluesky, Hacker News, Reddit, Mastodon — SYNTHETIC fixtures, defensive readers

The four conversation sources added later have **no live captures**: the
offline rule means `tests/fixtures/bluesky_*.json`, `hn_*.json`,
`reddit_*.json`, and `mastodon_*.json` are written to the documented API
shapes and are SYNTHETIC, in the same sense as
`web_search_response_with_citations.json`.

| Adapter | Endpoints read | Keyless? |
| --- | --- | --- |
| `sources/bluesky.py` | `GET public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed` | yes |
| `sources/hackernews.py` | `GET hn.algolia.com/api/v1/search_by_date`, `GET hn.algolia.com/api/v1/items/{id}` | yes |
| `sources/reddit.py` | `GET reddit.com/user/{u}/comments.json`, `…/submitted.json` | yes |
| `sources/mastodon.py` | `GET {host}/api/v1/accounts/lookup`, `…/accounts/{id}/statuses`, `…/statuses/{id}` | yes |

What this means in practice: the field names these adapters read
(`record.text`, `comment_text`, `link_title`, `in_reply_to_id`, …) are
documentation-derived, and a provider rename would degrade to fewer documents
with notes rather than to wrong attribution — every read is behind
`isinstance` checks and author verification, the lesson the GitHub capture
taught. The honest way to promote any of these from SYNTHETIC to CONFIRMED is
a live capture scrubbed the way `_scrub_github.py` records.

## GitHub, in one screen

Captured from the live API with an authenticated token on **2026-08-03**, then
scrubbed into `tests/fixtures/github_*.json` by
`tests/fixtures/_scrub_github.py`, which is kept as the record of exactly what
was changed: key names and nesting are real, identifiers and prose are not. The
raw captures were deleted.

| Endpoint | Status |
| --- | --- |
| `GET /users/{login}` | **CONFIRMED** 2026-08-03 |
| `GET /users/{login}/events/public` | **CONFIRMED** 2026-08-03, 100 events |
| `GET /search/issues?q=commenter:{login}+type:pr` | **CONFIRMED** 2026-08-03, 20 items |
| `GET /repos/{o}/{r}/issues/{n}/comments` | **UNVERIFIED** — element shape inferred from the identical comment object inside `IssueCommentEvent`, which *was* captured; that the path returns a bare array of them is assumed |

There is no live checker for GitHub. `scripts/verify_contract.py` exists because
twitterapi.io costs money to probe and drifts; GitHub is free to re-capture, and
the honest way to refresh these is to pull the three payloads again and re-run
the scrub. That cannot happen inside this sandbox — the agent proxy answers 403
to `api.github.com` for everything except `/rate_limit`, which is why the
captures were taken externally in the first place.

**Two findings from the capture are load-bearing and are asserted by tests** so
they cannot be quietly forgotten:

1. **No `PushEvent` carries `commits`.** 69 of 69, across a 50-push repo and a
   19-push hobby repo. The payload is `{repository_id, push_id, ref, head,
   before}` — also missing the documented `size` and `distinct_size`, and
   carrying an undocumented `repository_id`. Commit-message extraction is built
   and tested against the *documented* shape (`github_events_with_commits.json`,
   the one GitHub fixture that is synthetic and says so in its filename), and
   the adapter counts the gap rather than reporting nothing.
2. **A `search/issues` item is somebody else's writing.** `item.body` and
   `item.user` belong to whoever opened the pull request; 0 of 20 items were
   authored by the subject. The adapter never reads `body`.

Two measured limits, reported in every run's coverage block: `events/public`
reaches ~300 events and ~90 days, and the search API is limited to **30 requests
per minute**, separately from and far more tightly than core.

---

## Anthropic web search — CONFIRMED

**`POST /v1/messages` with `tools=[{"type": "web_search_20250305", "max_uses": 1}]`**

| | Status |
| --- | --- |
| Response envelope, result blocks, usage | **CONFIRMED** 2026-08-03 — nine live responses, 68 results |
| Citations, and therefore snippets | **CONFIRMED ABSENT** — 0 of 9 responses carried one |

Captured by `scripts/verify_search_contract.py --target dustinw
--capture-search captures/` and scrubbed into
`tests/fixtures/web_search_response.json` by `tests/fixtures/_scrub_search.py`,
which records exactly what was swapped. Key names, nesting, nulls and token
counts are real; identity and the opaque blobs are not. Platform hosts
(`x.com`, `linkedin.com`, `youtube.com`) are deliberately kept, because
`discovery.py` and `scoring.py` match them by name.

Every critical and important field held: `content`, `usage`,
`usage.server_tool_use.web_search_requests` (exactly 1 per call, nine times
over), the `web_search_tool_result` block, and `url` plus `title` on all 68
results.

### The finding: there is no snippet, and there never was

A `web_search_result` carries `url`, `title`, `page_age` and
`encrypted_content` — no snippet field, as documented. The snippet is built
from the model's **citations**, and all nine responses came back
`citations: null`.

That is not provider drift. `SEARCH_SYSTEM` instructs the model to run the
query and "reply with the single word: done", and a model that writes nothing
cites nothing. The snippet path is unreachable while that prompt stands, so
`corpus/search/providers.py` is correct in treating an empty snippet as normal,
and `web_search_response_with_citations.json` — SYNTHETIC, and named so — is the
only thing keeping the citation-reading code honest.

**What that cost.** Empty snippets meant every candidate scored as "the name and
nothing else" in the discover pass, which the common-name check read as eight
different people sharing the name. It stopped the phase before a single page was
fetched. The check now runs after the verification pass, on pages that were
actually read; see the README's "When the name is ambiguous" and
`corpus/search/verify.py`.

### Undocumented, observed, harmless

- **`caller`** on both `server_tool_use` (`null`) and `web_search_tool_result`
  (`{"type": "direct"}`).
- **`usage`** carries `cache_creation`, `inference_geo`,
  `output_tokens_details`, `service_tier`, and
  `server_tool_use.web_fetch_requests` beside `web_search_requests`.
- **Top level** carries `container`, `stop_details`, `stop_sequence`.

All additive. Nothing the parser reads moved.

### Two behaviours worth pinning

- **`page_age` is not always a date.** 53 of 68 were null; the rest were mostly
  `"June 23, 2013"`, but one was `"1 month ago"`. `parse_page_age` returns None
  for a value it cannot read rather than guessing, because a wrong
  `published_at` silently reorders a corpus.
- **A single result block held 6 to 10 results.** `RESULTS_PER_QUERY` is 8, so
  the tail of a broad query is dropped by us and not by the API.

---

## twitterapi.io

| | Status |
| --- | --- |
| `user_info` | **CONFIRMED** 2026-07-31 against the live API |
| `advanced_search` | **CONFIRMED** 2026-08-02 — a full ingestion ran end to end |
| `tweets_by_ids` | **PARTIALLY CONFIRMED** 2026-08-02 — batch ceiling measured at 50 |
| `last_tweets` | **UNVERIFIED** — not exercised by `corpus run`; documented shapes only |

> **What the 2026-08-02 live run established.** A real `corpus run` reached the
> end of ingestion against the live API. That exercised, on real payloads and
> without error: `advanced_search`, the `since_time:`/`until_time:` window walk,
> `_tweets_from`, `_cursor_from`, and `normalize_tweet`. The **only** mismatch
> was the `/twitter/tweets` batch ceiling — documented as 100, actually 50 — and
> it surfaced as a clean 400 during hydration rather than as corrupt data.
>
> That is a substantial upgrade in confidence: the sliding-window design, the
> array and cursor probing, and the tweet-object field names all survived
> contact with reality.
>
> **Read the confirmations narrowly, though.** "Ran without error" is weaker
> evidence for some of these than for others, because most of the readers
> involved cannot fail — they fall back. Specifically:
>
> - `_tweets_from` found *a* tweet array, but which of its four candidate
>   locations matched was not recorded. Same for `_cursor_from`.
> - `normalize_tweet` can only raise on an unparseable timestamp. That timestamps
>   parsed is real evidence; that `isReply`, `quoted_tweet`, or `retweeted_tweet`
>   were read *correctly* is not — a renamed field there produces a wrong `kind`,
>   silently, which is exactly the failure this document exists to catch.
>
> Those remain open below. A `--capture-raw` run would close them in one pass,
> and this environment still has no network route to `api.twitterapi.io` (the
> gateway answers `403` to `CONNECT`), so the captures have to come from a
> machine that does.

---

## `user_info` — CONFIRMED

**`GET /twitter/user/info?userName=<handle>`**

Envelope:

```json
{ "status": "success", "msg": "success", "data": { ... } }
```

`msg` is present. The original synthetic fixture omitted it, which is what first
exposed the fixtures as documentation-derived rather than captured.

Confirmed keys inside `data`:

```
id                name              userName          location
url               description       entities          protected
isVerified        isBlueVerified    verifiedType      followers
following         favouritesCount   statusesCount     mediaCount
createdAt         coverPicture      profilePicture    canDm
affiliatesHighlightedLabel          isAutomated       automatedBy
pinnedTweetIds
```

Two details worth stating explicitly because they are easy to get wrong:

- **`favouritesCount` uses the British spelling.** A reader who assumes
  `favoritesCount` gets `None` and no error.
- **`statusesCount` is the public post count.** Phase 2.3 reports it alongside
  the ingested count so a 400-of-53,901 run is visible at a glance instead of
  buried in `stop_reason`.

### Timestamp format — CONFIRMED

```
2010-08-27T20:13:59.000000Z
```

ISO 8601, six-digit microseconds, `Z` suffix. **Not** the legacy
`Mon Mar 03 12:00:00 +0000 2014` form the fixture assumed.

`parse_created_at` (`corpus/x/client.py:32`) handles it, but only by falling
through all three `strptime` formats to the `fromisoformat` fallback on the last
line. That it works is luck rather than design, and it is the same fallback path
that returns `datetime.now()` on total failure — see Phase 2.6, which is the bug
this format discovery exposed.

Both formats are now pinned by test:
`tests/test_wire_contract.py::test_both_confirmed_timestamp_formats_parse`.

---

## `advanced_search` — CONFIRMED (behaviour), field names still probed

**`GET /twitter/tweet/advanced_search?query=<q>&queryType=Latest&cursor=<c>`**

This is the endpoint the entire history walk runs on, and on 2026-08-02 a real
run walked a real account's history through it to completion. The table below is
still written as "what the code assumes" because the probe order means several
of these entries could be satisfied by a fallback rather than the first
candidate — see the note at the top.

| What | Code assumes | Where |
| --- | --- | --- |
| Tweet array | `tweets` (top level), falling back to `data.tweets`, `data`, `results` | `providers.py:_tweets_from` |
| Cursor | `next_cursor`, falling back to `nextCursor` | `providers.py:_cursor_from` |
| Has-more | `has_next_page`, falling back to `hasNextPage`, then `bool(cursor)` | same |
| Time bounds | `since_time:`/`until_time:` in **unix seconds**, inside the query string | `ingest.py:122` |
| Ordering | Newest-first within a page | `ingest.py` window walk |

The `bool(cursor)` fallback is worth calling out. If the provider always returns
a cursor value — even on the last page — the walk cannot tell that a window is
finished and will page to `--max-pages` (default 20) every single window, paying
for each. That is a cost bug that produces correct output, which is the kind
nobody notices.

---

## `last_tweets` — UNVERIFIED

**`GET /twitter/user/last_tweets?userName=<handle>&cursor=<c>`**

Same field assumptions as `advanced_search`, except the tweet array is expected
at `data.tweets` first rather than `tweets`. The two endpoints having different
nesting is itself a documented-only claim.

---

## `tweets_by_ids` — PARTIALLY CONFIRMED

**`GET /twitter/tweets?tweet_ids=<id>,<id>,...`**

| What | Status | Where |
| --- | --- | --- |
| Parameter | **CONFIRMED** — `tweet_ids`, comma-joined, no spaces. The 400 below is a batch-size complaint, not a parameter one, so the parameter was understood. | `providers.py:tweets_by_ids` |
| Batch ceiling | **MEASURED 50**, not the documented 100 | `providers.BATCH_LOOKUP_MAX` |
| Missing parent | assumed **absent from the returned array** — still unverified | `hydrate.py` renders `[unavailable]` |

### The batch ceiling — measured 2026-08-02

A live run failed hydration with:

```
400 {"detail":"max 50 tweet_ids per request, please batch into multiple calls"}
```

The documented figure was 100, and the code batched at 100 in three places. It
is now one constant, `BATCH_LOOKUP_MAX = 50` in `providers.py`, used by the
client-side cap and by the chunking in `client.py`, and pinned by
`test_the_batch_ceiling_is_fifty`.

Worth noting how this failed: as a clean 400 during hydration, *after* ingestion
had completed and `corpus.json` was already on disk. Nothing was corrupted and
nothing had to be re-fetched — which is the "paid data survives" property
working as designed.

The missing-parent assumption is load-bearing and untested. If a deleted or
protected parent comes back as an *object* carrying an error marker rather than
being omitted, `hydrate.py` will treat it as a real parent and splice a
placeholder — or an error string — into the `context` field that the synthesis
prompt reads as ground truth. `scripts/verify_contract.py` probes this
deliberately by including a known-bad id in its batch.

---

## Tweet object — UNVERIFIED

Read by `normalize_tweet` (`corpus/x/client.py:106`). Every lookup is a fallback
chain, so a rename produces a wrong value rather than an error.

| Purpose | Accepted names (in probe order) | Severity if all absent |
| --- | --- | --- |
| Id | `id`, `id_str`, `tweet_id` | **critical** — tweet dropped silently |
| Timestamp | `createdAt`, `created_at`, `timestamp` | **critical** — corrupts the walk |
| Text | `text`, `full_text`, `fullText` | **critical** — empty corpus |
| Author | `author`, `user` → `userName`/`screen_name`/`username` | important |
| Reply flag | `isReply`, `is_reply` | important |
| Parent id | `inReplyToId`, `in_reply_to_status_id_str`, `in_reply_to_id` | important |
| Quote | `quoted_tweet`, `quotedTweet` | important |
| Repost | `retweeted_tweet`, `retweetedTweet` | important |
| URL | `url`, `twitterUrl`, `tweet_url` | important |
| Entities | `entities.urls[]` → `expanded_url`/`unwound_url` | important |
| Media | `extendedEntities`/`extended_entities`/`entities` → `media` | important |
| Engagement | `likeCount`, `replyCount`, `retweetCount`, `quoteCount`, `viewCount` | optional |

**Critical** means the run produces zero or corrupt results and still exits 0.
The id and timestamp fields are critical for different reasons: a missing id
means `ingest.py:144` drops the tweet before dedupe ever sees it; a missing or
unparseable timestamp means `window_earliest` collapses to "now" and the
backwards walk advances one second per window while paying full price for every
page. That second failure is Phase 2.6.

`quoted_tweet`, `retweeted_tweet`, and `inReplyToId` are marked **conditional**
in `contract.py`: legitimately absent from posts they do not apply to, so they
are checked per-payload ("never observed across the sample") rather than
per-item. Asserting them on every tweet would flag every ordinary post and train
a reader to ignore the report.

---

## Open questions

Everything the Phase 1.2 capture must answer. Each line is a claim the code
currently makes without evidence.

**Structure**

- [ ] Actual URL path and exact query-parameter *names* for all four endpoints
- [ ] Where the tweet array actually lives — `tweets`, `data.tweets`, `data`, or something else — and whether it differs between `advanced_search` and `last_tweets` as assumed
- [ ] Is the envelope `{status, msg, data}` on the tweet endpoints too, as it is on `user_info`?

**Pagination**

- [ ] Cursor field name: `next_cursor` / `nextCursor` / something else
- [ ] Has-more signal: `has_next_page` / `hasNextPage` / absent
- [ ] **Is a cursor returned on the last page?** If yes, the `bool(cursor)` fallback in `_cursor_from` costs `--max-pages` requests per window
- [ ] Does the documented duplicate-cursor regression still reproduce on 2019–2022 history?

**Timestamps**

- [ ] Field name on *tweet* objects (may differ from user objects)
- [ ] Exact format — expect the ISO-with-microseconds form confirmed for `user_info`, but verify; the legacy form is still in the parser for a reason

**Semantics**

- [ ] How replies are marked: `isReply` boolean, presence of `inReplyToId`, or both
- [ ] How quotes are marked, and whether the quoted tweet arrives **inline** or needs a separate fetch
- [ ] How reposts are marked, and whether the original arrives inline
- [ ] Response for a **deleted or protected parent**: omitted from the array, or present with an error marker?

**Batching**

- [x] **Does `/twitter/tweets` accept comma-joined `tweet_ids`?** Yes — measured 2026-08-02. The 400 complained about the batch *size*, not the parameter.
- [x] **Real batch ceiling — is it 100, or lower, or higher?** **50.** Measured 2026-08-02 from `{"detail":"max 50 tweet_ids per request, please batch into multiple calls"}`. The documented figure of 100 was wrong.
- [ ] What happens on a batch containing one bad id: whole-request error, or partial results?

**The expensive one**

- [x] **Are `since_time:` / `until_time:` honoured as documented?** Effectively yes — measured 2026-08-02. A full ingestion completed against a real account, which it could not have done if the operators were ignored: the walk would have re-read the same recent page every window, dedupe would have eaten it, and the run would have stopped on the empty-window rule with a tiny corpus. Not yet confirmed at the level of "every returned timestamp falls inside the requested window" — `scripts/verify_contract.py` asserts exactly that and is the way to close it properly.

---

## Running the capture

```bash
corpus run --x <handle> --max-posts 50 --budget 0.15 --skip-synthesis --capture-raw captures/
```

Roughly $0.02. Pick a mid-volume account with steady posting and no long
silences, so the capture exercises replies and quotes without the hiatus
truncation of Phase 2.3 confusing the result.

Each call lands as `captures/{endpoint}_{timestamp}.json`:

```json
{
  "captured_at": "...",
  "request":  { "method": "GET", "url": "...", "path": "...", "params": {...} },
  "response": { "status": 200, "headers": {...},
                "body_sha256": "...", "body_redacted": false, "body": {...} }
}
```

- `body` is the parsed payload with nothing reordered, renamed, or filtered.
- `body_sha256` is over the original bytes, so verbatimness is provable —
  meaningful whenever `body_redacted` is `false`.
- Request headers are never captured; that is where the API key lives. Params,
  URL, and response headers are redacted. The body is redacted by exact secret
  value only, never by pattern, so captured post text is never rewritten.
- Response headers are kept deliberately: `Retry-After` and
  `x-rate-limit-reset` are what Phase 2.4's retry logic must honour, and their
  real names and formats can only be learned by looking at one.

Then, monthly:

```bash
python scripts/verify_contract.py            # ~$0.01
python scripts/verify_contract.py --dry-run  # free, prints the plan
```

It refuses to run under CI. The offline suite is the CI gate; a check that
spends money on every push is a check that gets deleted after the first
surprising invoice.

### The search capture

```bash
python scripts/verify_search_contract.py --target KEY --capture-search captures/
python tests/fixtures/_scrub_search.py     # captures/ -> tests/fixtures/
```

Roughly $0.13 at the default twelve queries. One file per query, holding the
whole message, what the parser made of it, and the usage that was billed.

**`captures/` is gitignored, and that is not incidental.** A capture holds a
real person's identifiers, other people's page titles, and a multi-kilobyte
`encrypted_content` blob per result that `scripts/check_secrets.sh` reads — as
it should — as high-entropy credential material. Committing one turns the
secrets gate red and puts real identifiers in the history. What belongs in the
repo is the scrubbed fixture and the script that produced it, exactly as
`gh_captures/` was handled. If a capture has to be shared, share the scrub
script's output.

---

## Changing this document

`corpus/x/contract.py` and this file must move together, and
`tests/test_wire_contract.py::test_unverified_endpoints_are_declared_unverified`
fails on purpose when an endpoint's verified status changes — so confirming an
endpoint and forgetting to update the docs is a red test, not a stale page.
