# Wire contract: twitterapi.io

What this tool assumes about the provider's responses, which of those
assumptions have been **observed on the wire**, and which are still **read from
documentation**. The distinction is the whole point of the document.

- Machine-readable form: `corpus/x/contract.py`
- Checked offline against fixtures: `tests/test_wire_contract.py` (every test run)
- Checked online against the provider: `scripts/verify_contract.py` (~$0.01, by hand)

| | Status |
| --- | --- |
| `user_info` | **CONFIRMED** 2026-07-31 against the live API |
| `advanced_search` | **UNVERIFIED** — documented shapes only |
| `last_tweets` | **UNVERIFIED** — documented shapes only |
| `tweets_by_ids` | **UNVERIFIED** — documented shapes only |

> **Why three of four are still unverified.** The capture run specified in
> Phase 1.2 has not been executed. Two independent blockers, both environmental:
> no `X_API_KEY` is present in this environment (there is no `.env` file), and
> the network policy denies egress to `api.twitterapi.io:443` — the proxy
> answers `403` to `CONNECT`, while a control host returns `200`. See
> [Open questions](#open-questions) for exactly what the capture must answer;
> the section is written as a checklist so filling it in is mechanical.
>
> Until then, the tweet-endpoint rows below state **what the code assumes**.
> That is useful — it is the thing to diff a capture against — but it is not
> evidence about twitterapi.io.

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

## `advanced_search` — UNVERIFIED

**`GET /twitter/tweet/advanced_search?query=<q>&queryType=Latest&cursor=<c>`**

This is the endpoint the entire history walk runs on, and it is entirely
unverified. Everything below is what the code does, not what the provider does.

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

## `tweets_by_ids` — UNVERIFIED

**`GET /twitter/tweets?tweet_ids=<id>,<id>,...`**

| What | Code assumes | Where |
| --- | --- | --- |
| Parameter | `tweet_ids`, comma-joined, no spaces | `providers.py:tweets_by_ids` |
| Batch ceiling | 100 per call, enforced client-side | `providers.py:173` |
| Missing parent | **Absent from the returned array** | `hydrate.py` renders `[unavailable]` |

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

- [ ] Does `/twitter/tweets` accept comma-joined `tweet_ids`?
- [ ] Real batch ceiling — is it 100, or lower, or higher?
- [ ] What happens on a batch containing one bad id: whole-request error, or partial results?

**The expensive one**

- [ ] **Are `since_time:` / `until_time:` honoured as documented?** If they are silently ignored, every window returns the same recent page, dedupe eats it, and `ingest.py` walks backwards forever paying full price for zero new posts. `scripts/verify_contract.py` checks this directly by asserting returned timestamps fall inside the requested window.

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

---

## Changing this document

`corpus/x/contract.py` and this file must move together, and
`tests/test_wire_contract.py::test_unverified_endpoints_are_declared_unverified`
fails on purpose when an endpoint's verified status changes — so confirming an
endpoint and forgetting to update the docs is a red test, not a stale page.
