# corpus

Finds a person's public writing wherever it lives, and reconstructs **how they
think** — the load-bearing beliefs that generate their positions, the moves they
make when they argue, and where they land on worldview axes even when they never
say so directly.

It is not a topic summary. A report that tells you what someone posts about is a
report you could have written from their profile page.

X used to be the primary and usually only source. It is now one optional source
among many, because it turned out not to be reliable: a live test against a public
account with 706 statuses and 308 followers returned **zero posts** from every
endpoint and query shape — `last_tweets`, `advanced_search` with and without time
bounds, `filter:replies`. The provider has no coverage for low-follower accounts.
The same person's blog, GitHub, and talks were all readable.

The conversation sources are the ones that close the gap X left: Bluesky reads
a full post-and-reply history over a keyless public API, Hacker News reaches
years of public argument, and Reddit and Mastodon do the same on their own
ground. All four merge into the same corpus, with the same attribution tiers
and the same reply-context rule the X pipeline is built around.

So you identify a person, the tool finds what they have published, and the same
synthesis pipeline runs over the combined corpus. `--x` is optional and a run with
no X anchor works end to end.

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
| `X_API_KEY` | only for X | twitterapi.io key, sent as the `X-API-Key` header. New keys get ~$1 trial credit, enough for ~6,000 posts. Not needed for a run with no X anchor. |
| `X_PROVIDER` | no | Provider selector. Defaults to `twitterapi_io`. |
| `X_BASE_URL` | no | Override the provider base URL (proxy, testing). |
| `X_MIN_REQUEST_INTERVAL` | no | Seconds between provider requests. Defaults to 5 — the free tier's measured QPS limit is one request per 5 seconds. Lower it on a paid tier; `0` disables the throttle. |
| `ANTHROPIC_API_KEY` | yes | Used for the map (`claude-haiku-4-5-20251001`) and reduce (`claude-sonnet-5`) passes, and for Phase 2 search via the server-side `web_search` tool. One key, no second vendor. |
| `SEARCH_PROVIDER` | no | Search provider selector. Defaults to `anthropic_search`. `exa` and `brave` are stubs naming what to add. |
| `GITHUB_TOKEN` | no | Raises the GitHub API rate limit from 60/hr to 5,000/hr. Discovery reads at most two public endpoints per target, which fits in the anonymous allowance. |
| `CORPUS_CACHE_DB` | no | SQLite cache path. Defaults to `~/.corpus/cache.db`. |
| `CORPUS_PROFILES` | no | Where saved targets live. Defaults to `./profiles.yaml` if present, else `~/.corpus/profiles.yaml`. |

---

## Usage

```bash
# Who are we reading? Anchors are what make everything after this safe.
corpus profile --name "Jane Smith" --employer "Acme Corp" --github jsmith --site https://janesmith.com
corpus profile --target janesmith          # show one
corpus profile                             # list them all

corpus run --target janesmith
corpus run --x paulg                                  # X only, as before
corpus run --github jsmith --site https://janesmith.com   # no X at all
corpus run --bluesky janesmith.bsky.social --hn jsmith    # public conversation, no X
corpus run --target janesmith --dry-run               # the plan and the queries, free
corpus run --target janesmith --no-search             # anchors and link-following only
corpus run --target janesmith --max-searches 20       # look harder, ~$0.01 a query
corpus run --x paulg --max-posts 5000 --since 2020-01-01 --budget 15
corpus run --x paulg --axes politics_and_ideology,defense_intel_natsec
corpus run --x paulg --resume out/paulg/2026-08-02   # pick up where a dead run stopped
corpus resynth out/paulg/2026-08-02                  # re-synthesize, no X fetch
corpus resynth out/paulg/2026-08-02 --render-only    # re-render only, zero API calls
corpus cache stats
corpus cache clear --keep-permanent
corpus cache vacuum
corpus budget log
corpus budget accuracy                  # how wrong --dry-run has been, historically

# Everything search found and could not prove lands in unconfirmed.md. Tick the
# boxes next to what really is theirs, then hand the file back:
corpus run --target janesmith --accept-unconfirmed out/janesmith/2026-08-03/unconfirmed.md
```

### The local web interface

```bash
uv run corpus serve        # then open http://127.0.0.1:8765/
```

A thin wrapper over the CLI, for running the tool without a terminal: a
new-run form (saved profiles are one click), the CLI's own `--dry-run`
estimate as a mandatory confirmation screen before anything is spent, a
one-at-a-time queue streaming the same progress lines the terminal prints,
run history, an HTML report view (the verdict page opens first; the evidence
is one click away), and an append-only audit log recording who ran what,
when, at what spend, and the required free-text reason why.

Every run it starts is a `corpus run` subprocess: same `.env`, same budget
hard stop, same output. It binds to 127.0.0.1 only and refuses anything
else — there is no authentication because there is deliberately no network
exposure. Keys never reach the browser.

### Options that matter

| Flag | Default | Notes |
| --- | --- | --- |
| `--target KEY` | unset | A saved identity card from `profiles.yaml`. Flags override it and never write back. |
| `--x` / `--github` / `--site` / `--substack` | unset | Anchors: things confirmed to be theirs. At least one identifier is required. |
| `--bluesky` / `--hn` / `--reddit` / `--mastodon` | unset | More anchors: public conversation on Bluesky, Hacker News, Reddit, and Mastodon (`@user@instance`). Keyless public APIs, plain HTTP, free. |
| `--name` / `--employer` / `--role` / `--location` | unset | Scored against what discovery finds. `--location` disambiguates a common name. |
| `--discover` / `--no-discover` | on | Follow links out from the anchors. Free, never fatal. Anchors are read either way. |
| `--max-fetches` | 25 | Ceiling on discovery's plain-HTTP requests. Not a money guard — there is no money here — but a guard against a hostile link graph. |
| `--search` / `--no-search` | on | Phase 2: find sources the anchors do not reach. Costs ~$0.01 per query. Never ingests what it cannot verify. |
| `--max-searches` | 12 | Billable queries per run, reported in the estimate and the report. Cached queries are free and do not count. |
| `--max-verify-fetches` | 40 | Pages fetched to verify search candidates. Free, plain HTTP. Retries 429/5xx with backoff, spaces requests per host, and identifies itself honestly; roughly half of candidate pages still refuse, and the report now counts *where* reads were lost (403 vs timeout vs DNS, per phase). |
| `--reader-fallback` | **off** | For verification pages that 403 a direct fetch, allow ONE fallback through a text-extraction reader service (r.jina.ai; `READER_API_KEY` raises its rate limit). **Enabling this sends every fallback URL to a third party.** Recovered pages are extracted text, not original HTML — authorship signals are weaker, and both the document's attribution basis and the report's coverage block say so. Never used for people-search hosts or excluded URLs. Also `CORPUS_READER_FALLBACK=1`. |
| `--accept-unconfirmed PATH` | unset | Read back an edited `unconfirmed.md`: ticked entries are ingested as `corroborated`, unticked ones go into the card's `exclude` list. |
| `--rss URL` / `--url URL` | unset | Repeatable, read directly, not crawled. Anchor-attributed. |
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
| `--dry-run` | off | Print the discovery plan and the estimate, then stop before any *paid* fetch. Discovery and the free sources still run — that is what makes the estimate mean anything on a run with no X anchor. |
| `--resume PATH` | unset | Continue a previous run from its `run.json`. |
| `--axes a,b,c` | all | Which worldview axes to place the subject on. Names come from `corpus/profiles.yaml`; an unknown name is an error, not a silent drop. |
| `--map-model` / `--reduce-model` | haiku-4.5 / opus-5 | Map is extraction; reduce is judgment. Do not downgrade reduce. |
| `--map-effort` / `--reduce-effort` | medium / high | Only sent to models that implement `effort` — Haiku rejects it outright. |
| `--no-filter` | off | Keep low-signal documents (bare acks, link-only posts, short fragments). Also raises the tier, since the tier follows what the model sees. |
| `--render-only` | off | `resynth` only. Rebuild `report.md` from an existing `synthesis.json`. No API calls, no spend. |
| `--log-format` | text | `text` or `json` (one object per line). |
| `--verbose` / `--quiet` | off | Add phase and elapsed time / warnings and errors only. |
| `--capture-raw DIR` | unset | Dump every raw provider response verbatim, before normalization. |
| `--capture-search DIR` | unset | Dump every raw search response verbatim, before it is interpreted. |

---

## Finding the writing

### The failure mode this is all built around

Automated search on a name returns other people. A tool that produces confident,
well-formatted reports and silently attributes a stranger's blog post to the
target is **worse than no tool**, because the output is indistinguishable from a
correct one.

Every decision below serves attribution confidence. Where coverage and
attribution conflict, attribution wins.

### The identity card

You supply the anchors; everything discovered is scored against them.

```yaml
# profiles.yaml
targets:
  janesmith:
    name: Jane Smith
    employer: Acme Corp
    role: VP Engineering
    location: Seattle          # optional, disambiguates a common name
    anchors:                   # confirmed to be her
      x: janesmith
      github: jsmith
      site: https://janesmith.com
      substack: janesmith.substack.com
      bluesky: janesmith.bsky.social
      hn: jsmith
      reddit: janesmith
      mastodon: "@janesmith@mastodon.social"
    exclude:                   # known false positives
      - https://linkedin.com/in/jane-smith-attorney
```

Written by `corpus profile` and safe to hand-edit; saving preserves the rest of
the file. Anchors are validated where they are written down rather than where
they are used — an anchor reaches a URL through several callers, and each of them
getting it right independently is how one of them gets it wrong. An unknown
anchor kind is an error naming the valid ones, the same rule an unknown axis name
follows.

The file is deliberately *not* `corpus/profiles.yaml`, which ships in the wheel
and holds the axes. Targets are personal notes about real people; a `pip install
-U` must not be able to delete them.

### Attribution, on every document

| Tier | Means | Ingested by default |
| --- | --- | :-: |
| `anchor` | A URL or handle you supplied. Certain. | yes |
| `linked` | Reached by following a link from an anchor. | yes |
| `corroborated` | Found by search, naming them (or their handle), with 2.0 points of agreeing evidence: one strong self-declaration, or agreeing moderate signals. | yes |
| `name_match` | The name matched and nothing else. | **no** |

`name_match` candidates are recorded in `discovery.json` with what matched and
what did not, written to `unconfirmed.md` as an editable checklist, and counted
in the report. `report.md` shows the
attribution mix in its coverage block — and stays silent when everything is an
anchor, because a line reading "100% certain" on every report teaches the reader
to skip the one where it matters. A finding resting *only* on corroborated
evidence is flagged at the citation.

### Phase 1: link-following

No search, highest confidence, and it costs nothing. From each anchor:

- **X bio** URL and description links, plus the pinned post — read out of the
  profile the run already paid for, so the bio itself is free.
- **GitHub** profile `blog` field and bio, plus the profile README. A `github`
  anchor is also read as a source in its own right — see below.
- **Substack** about page.
- **A personal site**: its outbound links, its declared
  `<link rel="alternate">` feed, and failing that up to six probes —
  `/feed`, `/rss.xml`, `/atom.xml`, then `/blog`, `/writing`, `/essays`.

A site that advertises its own feed costs one request; only a site that does not
gets probed. Once a feed is in hand the homepage is dropped, because a homepage
with the feed in hand is a nav bar with a photograph.

**What makes a link `linked` rather than a lead.** "Reached from an anchor" is not
enough on its own: a personal site links to other people's blogs constantly. So
surfaces are split in two.

- **Declared** surfaces are fields a person filled in about themselves — a GitHub
  `blog` field, an X bio URL. A link there *is* a claim of ownership, so it is
  `linked` unconditionally.
- **Page** surfaces are prose — a profile README, an about page, a homepage. A
  link there is `linked` only with a corroborating signal (it sits on a host the
  card already anchors, or the host or path carries their name) and is otherwise
  held as `name_match`.

Without that split, a profile README — mostly other people's projects, badges,
and papers — would put half of someone's reading list in the corpus.

Discovery is never fatal. A dead host, a redirect loop, or a hit fetch cap
degrades the corpus and is reported; it does not end a run, because discovery
happens before anything has been paid for.

### Phase 2: search

Phase 1 only reaches what the anchors already point at. Phase 2 looks for what
they do not — and it is the phase that can find the wrong person, so almost all
of it is machinery for not doing that.

**Never a bare name.** Queries are built from the identity card, and a query
whose field is missing is skipped rather than emitted with an empty slot:

```
"Jane Smith" "Acme Corp" Seattle      # location boosts the two highest-yield queries
"@janesmith"                          # people cite handles
jsmith github
"Jane Smith" "VP Engineering"
"Jane Smith" blog OR essay OR writing Seattle
"Jane Smith" author OR byline
"Jane Smith" interview OR podcast OR transcript
"Jane Smith" talk OR keynote OR conference
"Jane Smith" "Acme Corp" site:news.ycombinator.com
```

Precision first, because `--max-searches` truncates from the end: a cap of 3
buys the employer and handle queries, not the conference sweep. Results are
cached by query string **permanently** — iterating on the scoring must not cost
a dollar a lap.

**Two passes, because a snippet is not evidence.** A snippet is 150 characters
a model chose to quote on a page nobody has read, and every strong signal lives
in the page. So *discover* scores snippets only to decide what is worth
fetching, and *verify* fetches the survivors and scores those. Only the second
pass can promote anything; a candidate that was never fetched is held no matter
how good its snippet looked.

**Scoring is deterministic Python** — no model call, no network, no clock. The
file that decides whether a stranger's essay is attributed to the subject can be
read, argued with, and tested for free.

| Signal | Weight |
| --- | --- |
| Page links to a known anchor from its own author/byline block (or `<address>`), or while declaring the anchor handle as its own | strong |
| Page links to a known anchor from body prose only | moderate |
| Domain is or subdomains an anchor domain | strong |
| Structured author metadata matches the name (`<meta name="author">`, JSON-LD, OpenGraph, feed `<author>`) | strong |
| Two or more anchor handles co-occur on the page | strong |
| Employer named on the page | moderate |
| Role matches | moderate |
| Name in the byline rather than the body | moderate |
| Location matches | weak |

The anchor-link split is the declared-field-versus-page-prose distinction the
attribution model already treats as load-bearing, applied one level down: a
GitHub `blog` field is a self-declaration, a link in a README body is prose.
A page that carries the link while identifying itself is very likely theirs; a
stranger's post *about* them links their GitHub too. `<header>` and `<footer>`
deliberately do not count as self-identification: blogrolls and "people I
read" lists live in footers, and the pages that link a researcher's homepage
are mostly other people's blogs.

The negatives are the half that makes it work, because **absence of confirmation
is not the same as presence of contradiction** and a scorer with only positive
signals accepts the wrong person cheerfully — it never finds a reason not to.

| Negative | Effect |
| --- | --- |
| Matches an `exclude` entry | reject |
| Aggregator, directory, or people-search domain | reject |
| Name appears only as a citation of someone else's work | reject |
| Page is *about* them rather than *by* them | recorded as context, never a corpus document |
| Page states a **different** employer for this name | strong — demotes to held |
| Page states a different location or a clearly different field | moderate — demotes to held |

Negatives are checked *before* the positives are counted, and any unresolved one
demotes: a page with three strong signals and one contradiction goes to a human.
"Unresolved" is doing real work there — a page naming a different employer is a
contradiction *unless* it also names the right one, in which case it is a page
that mentions two companies. Each negative names what would resolve it.

Signals are weighed, not counted: a strong signal is worth 2 points, a
moderate 1, a weak 0.5, and 2.0 points with no negatives → `corroborated`, and
ingested. One strong self-declaration is enough; so are two moderate
agreements; anything less → `name_match`, and held. (A count of two was
backwards — it promoted two moderate agreements while holding a page whose own
byline links their GitHub.)

Points promote only past the **identity precondition**: the page must attach
the target's identity — their full name, their specific handle, or a page that
identifies itself as theirs (their anchored host, or an anchor link in the
page's own author furniture). Employer, role and location corroborate an
identity that is already established; they cannot establish one. "OpenAI" in a
sentence about someone else is not evidence about this person, so a page
naming nobody is capped at held whatever its points total.
**`linked` is unreachable from search by construction**: it means "reached from
a declared field on an anchor", and no quantity of search evidence turns into a
self-declaration.

Signals that are two views of one fact count once, at the strongest member's
weight. A `<meta name="author">` tag and a visible "By Jane Smith" are usually
the same byline rendered twice, and scoring both would manufacture 3.0 points
out of a single claim.

### When the name is ambiguous

If two or more independent hosts **whose pages were read** attach conflicting
identity facts to the name — a different employer, a different professional
field, a different location than the card's — the phase refuses to guess:
nothing is ingested, everything found is held for a human, and the refusal
names the actual conflict ("taxblog.example: the page puts this name at 'Beta
Industries', not Acme Corp"). A tool that guesses between two people sharing a
name is a liability.

Conflict, not domain count. An earlier version counted distinct domains that
matched the name and nothing else, and that inference is backwards for a
public figure: many domains mentioning one name is evidence of reach, not of
several people sharing it. It fired on Simon Willison — 54 results, 25 pages
read, every candidate silently held — and the better known the target, the
more certainly it misfired. Domain diversity is what search working *well*
looks like; ambiguity is contradiction.

Two consequences follow. Where the card supplies no employer, role or
location, contradiction cannot be established, and the check declines to run
and says so in the report rather than falling back to counting domains. And
the check still runs only on **fetched** pages, learned the expensive way on
2026-08-03: snippet scores can neither confirm nor contradict anything, and a
gate fed snippets once concluded a researcher's own academic profiles were
several different people before reading a single page. Asking after the
fetches costs at most `--max-verify-fetches` plain HTTP requests, free and
cached; asking early cost a whole phase.

### The unconfirmed workflow

Everything held lands in `out/{target}/{date}/unconfirmed.md`, one checkbox per
candidate with what found it, what matched, what did not, and what the page says:

```markdown
- [ ] https://someblog.example/about
      Found by: query `"Jane Smith" "Acme Corp"`
      Matched: their name is in the byline
      Did not match: the page puts this name at 'Beta Industries', not Acme Corp
      Snippet: ...
```

Hand it back with `--accept-unconfirmed PATH`. **Ticked** entries are ingested as
`corroborated` with basis `user-confirmed` — a person is better evidence than any
signal in the scorer, which is why the path exists. **Unticked** entries are
written into the card's `exclude` list so they never resurface.

That asymmetry makes an *unedited* file dangerous, since it would reject every
candidate forever. So the file says so at the top, and a run given a file with
nothing ticked asks before acting on it.

### An empty corpus is a finding, not a crash

When every source completes without error and nothing could be attributed, the
run **exits 0** and writes a `report.md` that says so plainly: which sources
were tried, what each returned, and how many candidates search found and held
(with a pointer to `unconfirmed.md` — "we found things and could not confirm
they were theirs" is a different answer from "we found nothing"). Where the
card carried only one anchor, the report says that adding a GitHub username or
a personal site is what would change the outcome. The web interface shows this
as its own neutral **no corpus** status, distinct from `done` and from
`failed`. A non-zero exit still means something actually went wrong: a source
errored, the budget stopped the run, or a call failed.

### What is deliberately not built

- **No LinkedIn scraping.** Automated access is blocked and against their terms.
- **No personal Facebook profiles.** Not public data.
- **No authenticated scraping of any platform.** No session cookies, no
  logged-in session reuse.
- **No aggregation of non-public personal data** — addresses, phone numbers,
  family, financial records, data-broker output.

This tool reads what a person chose to publish under their own name. That
constraint is not a limitation to route around; it is what makes the tool
legitimate. An anchor or a link to one of the above is refused with the reason
rather than quietly dropped.

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
| Web search (Anthropic server-side tool) | $10 / 1,000 searches, plus the tokens the results consume |
| `claude-haiku-4-5-20251001` (map) | $1 / $5 per MTok |
| `claude-sonnet-5` (reduce) | $2 / $10 per MTok (introductory, through 2026-08-31; $3 / $15 after) |
| `claude-opus-5` (`--reduce-model claude-opus-5`) | $5 / $25 per MTok |
| Prompt cache write / read | 1.25× / 0.10× the input rate |

Sonnet 5 introductory pricing is date-aware in code, so the printed spend matches the
invoice instead of being conveniently vague.

### Worked examples

Assumes ~50% of posts are replies or quotes needing one extra read to hydrate the
parent, ~120 tokens per document, ~30k-token map chunks, and one reduce call whose
output allowance covers thinking (the reduce model thinks by default, and thinking bills
as output). These are the numbers `--dry-run` prints, computed from the price table above
at Sonnet 5's standard rate — through 2026-08-31 the introductory rate makes the
Anthropic column a little lower, and the code always charges the rate for the day.

| Posts | Tweet reads | X data | Map chunks | Anthropic | **Total** |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 750 | $0.11 | 2 | $0.24 | **~$0.35** |
| 1,000 | 1,500 | $0.23 | 4 | $0.33 | **~$0.56** |
| 3,000 | 4,500 | $0.68 | 12 | $0.66 | **~$1.34** |
| 10,000 | 15,000 | $2.25 | 40 | $1.84 | **~$4.09** |

This estimate is not the reservation. `--dry-run` answers "what will this run cost";
`Budget.reserve` answers "can this specific call be covered right now" and charges the
full `max_tokens` because a reservation that guesses low is not a ceiling. The reduce
call therefore reserves its worst case regardless of what it ends up using — around
$0.50 with the default `claude-sonnet-5` reduce (about $0.35 during the introductory
window), around $0.80 with `--reduce-model claude-opus-5` — which is why very small
budgets refuse synthesis in strict mode.

Three things move these numbers most: hydration ratio (a reply-heavy account costs more
because every reply needs its parent), `--reduce-effort` (the reduce call is a small
fraction of tokens but the most expensive per token), and the map model — map is the
bulk of the tokens, which is why it runs on Haiku.

The default `--budget 10.00` comfortably covers a 10,000-post run. `--dry-run` prints
the estimate for a specific target after one profile lookup (~$0.0002).

### The split by phase

Every estimate now breaks four ways, because a single total cannot tell you which
phase surprised you. At 1,000 posts:

```
    discovery (plain HTTP):   $0.000  (4 request(s))
    fetch — X data:          ~$0.225
    map:                     ~$0.150
    reduce:                  ~$0.177
    total:                   ~$0.552 of $10.00 budget
```

Map and reduce are roughly even at that size, but reduce barely moves with
corpus size while map scales linearly — which is exactly why the split is
worth printing.

**Discovery is $0.000 and that is not rounding.** Phase 1 is plain HTTP against
public pages, cached, with one exception: the pinned-post read goes through the
metered X API, which is why it only happens when a client is available and never
under `--dry-run`.

Discovery and every non-X source are read *before* the estimate and before the
spend confirmation. They are free, and on a run with no X anchor an estimate that
ignored them would be an estimate of nothing.

Budget **$1.50–$3.00 per target** with search on. X-only runs are still under a
dollar, and `--no-search` keeps a run there.

Search is the most expensive thing per call in the tool: $0.01 a query means the
default `--max-searches 12` is ~$0.12 in fees before a token. It is also the only
cost a cache can remove entirely, which is why results are cached by query string
permanently — a re-run at a higher cap pays only for the queries it has not
already run.

### Where the cost was taken out

- **Constrained decoding restored.** The reduce schema is 3,251 bytes, under the grammar
  limit the old 4,826-byte schema blew through — see the third regression below. The
  fallback stays, because the limit is not ours to control.
- **Haiku on map.** Map is extraction, and extraction does not need judgment.
- **Low-signal filter.** Documents that cannot carry an argument never reach a billed
  token.

### The budget is enforced before the call, not after

`--budget` is a ceiling, not a tripwire. Every billable call is *reserved* first, and a
call that cannot be fully covered is refused rather than made and regretted. Reservations
are held for the duration of a call, so the four concurrent map slices cannot collectively
overshoot.

One consequence worth knowing: the reduce call reserves its **worst case**, which is
the reduce model emitting all 32,000 output tokens — about **$0.50** for the default
`claude-sonnet-5` (about $0.35 during the introductory window), about **$0.80** with
`--reduce-model claude-opus-5`. In `strict` mode a budget below roughly half a dollar
will therefore refuse synthesis outright, even though the call would probably have cost
a fraction of that. That is the guarantee working: it cannot promise not to exceed $0.25
when the model *may* spend $0.50. Use `--budget-mode advisory`
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
accepted and 10 (3,809) are not. The schema at the time was 4,826. The map schema, at
951 bytes, is unaffected — which is why the map stage always worked and only the reduce
failed.

The reduce asks for constrained decoding and, if the schema is refused, retries without
it and puts the schema in the prompt instead. That retry is free, because the API refuses
the schema before generating anything, so it does not consume one of the two billed
validation attempts. The real guarantee was never the grammar: every reduce output is
validated against the pydantic model regardless, and a failure retries once with the
error appended. When the fallback fires, the run says so and `report.md` records it.

**The cognition-first schema is 3,251 bytes and fits**, so constrained decoding is the
normal path again and the grammar physically cannot emit the markdown fences that used to
cost a full billed generation. `tests/test_schema_size.py` fails the build at 3,400 so a
future field addition breaks a test rather than quietly costing money in production.

The fallback stays anyway, and this is the interesting part: it is now the path nothing
in normal operation exercises. The size limit belongs to the provider, and the next field
added could put us back over it — in production, after ingestion has been paid for. Its
tests in `tests/test_synthesize.py` are therefore the only thing keeping it honest, and
`_strip_code_fences()` runs before every validation regardless, because a model asked for
JSON in a prompt is exactly the one that wraps it in backticks.

---

## Pipeline

```
ingest → hydrate → signals → filter → map → reduce → render
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

### The low-signal filter is structural, never topical

Before synthesis, pure Python drops documents that cannot carry cognition: bare
acknowledgements, link-only posts with no commentary, and very short standalone fragments
with no parent. A short post *with* context is never dropped — "Completely backwards." is
two words and, given its parent, one of the most revealing documents in any corpus.
Threads are never dropped.

Nothing is ever filtered by subject matter. That someone treats hobby minutiae with the
same analytic seriousness as politics is itself a cognitive tell, and a topic filter would
delete the evidence for it. The drop count and reasons land in the report's coverage
block; `--no-filter` disables it.

### What the corpus is big enough to support

Nothing used to vary with document count, which is backwards: a thin corpus is
exactly where inference is most likely to confabulate. The tier is computed in
Python from the **post-filter** count and injected as ground truth — the model
is never asked to assess its own evidence base, since self-assessment is the
thing under suspicion.

| Tier | Documents | What changes |
| --- | ---: | --- |
| **thin** | under 40 | Inference switched off. `inferred` and `reasoning` are emptied on every axis, `blind_spots` and `evolution` come back empty, `coverage.confidence` is forced to `low`. Beliefs survive with their evidence, but `role` and `generates` are cleared — see below. |
| **moderate** | 40–149 | Inference allowed, but every inference must rest on **3 distinct documents** instead of 1. One striking post is an anecdote, not a pattern. |
| **rich** | 150+ | The behaviour the report was designed around. |

**The cut runs through `core_model`, not around it.** A belief traced to real
posts is a sourced claim and survives with its evidence. But `role` and
`generates` describe where a belief sits relative to the others, and that is an
inference about structure — so at thin they are cleared to `role:
"unclassified"` and an empty `generates`. Not `held_lightly`: that asserts they
voice the view but do not defend it, which a thin corpus does not know either,
and forcing it would trade one confabulation for another. `"unclassified"` is
deliberately absent from the wire schema's enum, so grammar-constrained decoding
physically cannot produce it — the value means "code cleared this" and nothing
else.

The report renames that section **"Beliefs, without the structure"** and says so
in a line above the list. A flat list under "The generating model" would read as
a considered tree that happens to have no branches, which is a stronger claim
than the corpus supports, made by omission.

Both halves are real: the reduce prompt states the tier's rules so the model
does not spend a generation writing what will be deleted, and `prune_unsourced`
deletes it anyway if it does. The report says which tier it ran at, and a thin
one leads with a section explaining what was switched off and how to fix it.

`--dry-run` warns *before* the money is spent when the projected corpus is under
the floor, and names the secondary sources — they merge into the same corpus and
cost nothing, being plain HTTP rather than a metered API.

### Two inference tiers, always held apart

The report makes claims the subject never made — that is the point — but it never blurs
them together.

- **`stated`** is what they actually said. It traces to real document ids or it is
  dropped, the same rule the tool has always enforced.
- **`inferred`** is what follows from it. It requires a `reasoning` chain from specific
  posts to the conclusion, plus a `confidence` value — and, below the rich tier, a
  minimum number of distinct sourced documents.

**The reasoning is the evidence for the inference.** An inferred conclusion whose chain is
missing, too short to be a chain, or a restatement of its own conclusion is deleted in
`prune_unsourced()`, exactly as an unsourced claim is — and the stated tier survives on
its own sourcing rather than being collateral damage.

### `signal: "none"` is a result, not a gap

Every requested axis appears in the report. An axis the corpus cannot speak to reports
`no signal` and cites nothing; if the model writes content there anyway, the content is
cleared and the clearing is logged. An axis claiming signal without valid evidence ids is
demoted to `none`.

A confabulated axis is worse than an absent one, because it is indistinguishable from a
real finding.

### The rest of the rules are enforced, not just requested

The system prompt states the rules; `synthesize.py` then makes them true in code:

- **Every `evidence_ids` entry is checked against the real corpus.** Findings citing an
  id that does not exist are dropped, and the drops appear in the report.
- **Evidence is capped at three ids per claim**, so the report reads as analysis with
  citations rather than a citation dump with commentary.
- **Every count with a counterpart in `signals.json` is overwritten** with the computed
  value.

Reduce output has its markdown fences stripped, then is validated against a pydantic
model. On failure it retries once with the validation error appended; on a second failure
it dumps the raw output to `reduce_raw_output.txt` and exits non-zero.

### Configurable axes

`corpus/profiles.yaml` defines the worldview axes and, for each, a `probe` injected
verbatim into the reduce prompt. `--axes politics_and_ideology,defense_intel_natsec`
restricts a run. Nothing is hardcoded, and an unknown axis name is an error listing the
valid ones — a typo must not silently produce a report that looks complete.

---

## Output

Written to `out/{handle}/{YYYY-MM-DD}/`:

| File | Contents |
| --- | --- |
| `report.md` | The generating model, the reasoning machinery, the axes (including the silent ones), what moved, what is unresolved, and how to misread it. Every claim hyperlinked to its source post, coverage caveats in a callout at the top, spend summary at the bottom. |
| `synthesis.json` | The validated schema, for piping into downstream drafting. |
| `corpus.json` | Every hydrated `Document`, each carrying its attribution and the basis for it. |
| `signals.json` | The computed metrics. |
| `discovery.json` | The identity card, every candidate found, why each one was believed — and the `name_match` candidates that were held back rather than ingested. |
| `unconfirmed.md` | Every search candidate that could not be verified, as an editable checklist. Hand it back with `--accept-unconfirmed`. |

Plus `run.json` (resume state) and `run_meta.json` (the corpus tier, and what the
enforcement dropped and why).

`corpus.json` and `signals.json` are written **before** synthesis runs, so a synthesis
failure never costs you the data you paid for.

`corpus resynth <dir> --render-only` rebuilds `report.md` from an existing
`synthesis.json` with **zero API calls**, so iterating on the report's shape is free. A
`synthesis.json` written by the pre-cognition schema cannot be re-rendered — it lacks
fields the new report needs — and gets a migration message pointing at plain
`corpus resynth`, which regenerates it from the unchanged `corpus.json` with no X spend.

---

## GitHub

For a technical person with no X presence this is often the only place their
reasoning is written down in public, and the valuable part is not the repo list.
It is the argument in a review comment and the justification in a commit message
— technical reasoning under disagreement. So `sources/github.py` reads no repo
metadata at all, and comments come first.

Two paths, and neither needs a key:

1. **`/users/{login}/events/public`** — review comments (with the diff hunk kept
   as `context`, for the same reason a reply keeps its parent post), issue and PR
   comments, and commit messages from `PushEvent`.
2. **`/search/issues?q=commenter:{login}+type:pr`**, then each thread's
   `comments_url` — reaches back further than the events feed, filtered to the
   comments the subject actually wrote.

`GITHUB_TOKEN` raises the rate limit from 60/hr to 5,000/hr. Without it the
adapter opens 8 threads instead of 25, because 25 thread reads would be a third
of the anonymous hourly budget spent on one person.

### What a live capture changed about the design

Three payloads were pulled from the live API with a token on 2026-08-03 and
scrubbed into `tests/fixtures/github_*.json`. Two findings in them are
load-bearing, and both would otherwise have been silent:

- **No `PushEvent` carries commit messages.** All 69 in the capture had the
  payload `{repository_id, push_id, ref, head, before}` — no `commits`, no
  `size`, no `distinct_size`, and an undocumented `repository_id`. Consistent
  across a 50-push repo and a 19-push hobby repo, so not size-related
  truncation. The extraction and the wip/typo/merge filter are implemented and
  tested against the documented shape; `push_events_without_commits` counts the
  gap, so a run says "69 pushes, 0 with messages attached" rather than producing
  nothing and looking like an account that never commits.
- **A `search/issues` item is not the subject's writing.** Each item is the pull
  request they *commented on*, so `item.body` and `item.user` belong to whoever
  opened it — **0 of 20 items in the capture were authored by the subject**. An
  adapter reading `item.body` would attribute twenty strangers' PR descriptions
  to the target. So `body` is never read at all: the search result is a list of
  pointers, and the comments are fetched per thread and filtered by author.

`corpus/sources/github_contract.py` states every field name and nesting the
adapter depends on, with a severity and a what-breaks note each.
`tests/test_github_wire_contract.py` checks the fixtures against it on every
run, and cross-checks in both directions — a field marked critical that the
adapter never reads fails, and a field the adapter reads that the contract never
mentions fails too.

### Two limits, reported in every coverage block

- **`events/public` reaches ~300 events and ~90 days.** Commit and comment
  coverage from that path is recent activity, never history. A reader who has to
  infer that from a suspiciously short date range will infer something about the
  person instead.
- **`search/issues` has its own rate limit of 30 requests per _minute_**,
  separate from and far tighter than the core limit. One search runs per target
  and it is not paginated.

---

## Sources other than X

One file each in `corpus/sources/`, merging into the same corpus.

- `--bluesky HANDLE` — an anchor. Full post and reply history from the public
  AppView API (`public.api.bsky.app`), keyless. Reply context arrives inline
  in the feed — the hydration the X pipeline pays for, free — and a deleted
  or blocked parent becomes `[unavailable]`, never a dropped document.
  Reposts are skipped and counted; consecutive self-replies are stitched into
  threads, same as X.
- `--hn USER` — an anchor. Stories and comments from the Algolia HN API,
  keyless, reaching back years. A comment on another comment gets its parent
  hydrated from the `items` endpoint — capped at 25 lookups, cached
  permanently because old HN items never change — and a comment on the story
  itself keeps the story title as its context.
- `--reddit USER` — an anchor. Public comments and submissions from the
  keyless JSON listings. A comment's context is its submission title, inline
  at no extra cost; parent comment text is not hydrated (each would be a live
  request against a site that rate-limits anonymous readers), and the gap is
  stated rather than hidden.
- `--mastodon @user@instance` — an anchor. Public and unlisted statuses from
  the instance's keyless API; anything more private is skipped and counted,
  per document, never worked around. Replies hydrate their parents under the
  same 25-lookup cap, boosts are excluded at the API, and self-reply threads
  are stitched.
- `--substack DOMAIN` — an anchor. Paginates `/api/v1/archive`, fetches bodies via
  `/api/v1/posts/{slug}`, falls back to `/feed`. Paywalled posts keep title and
  subtitle only. Its about page is crawled like any other anchor.
- `--rss URL` — any feed: Medium, Ghost, WordPress, personal blogs. Repeatable,
  read directly, not crawled.
- `--url URL` — a single page, readability-style extraction. Repeatable, not crawled.

All are free (plain HTTP, no metered API) and non-fatal: any failure —
including a transport error, which is not a `SourceError` — is converted,
logged, and skipped. Adding a platform should mean one new file in
`sources/`. If it requires editing `synthesize.py`, the abstraction is wrong.

### What the shared plumbing guarantees

Every adapter is built on the same pieces in `sources/base.py`, so the rules
hold everywhere rather than being re-argued per platform: cached JSON GETs
that degrade to notes instead of exceptions (`JsonReader`), one HTML-to-text
extractor, dates that are real or `date_unknown` and never the clock, and
`collapse_self_threads` for stitching self-reply chains into one document.
Attribution is stamped by the caller, not the adapter — the same Bluesky
fetch is `anchor` when you typed the handle and `linked` when discovery found
it in a GitHub bio.

---

## Scope boundaries

Public content published under the person's own name only. No private or protected
accounts, no follower graph enumeration, no DMs, no authentication bypass, no paywall
circumvention, no LinkedIn or Facebook scraping, and no aggregation of non-public
personal data. An adapter that would require any of that fails with an explanation
instead. See [What is deliberately not built](#what-is-deliberately-not-built).

---

## Development

```bash
make install     # uv venv + the package + pytest, ruff, mypy
make check       # lint, format, types, secrets, tests — the gate
make coverage    # per-module floors on the money and history paths
```

975 tests, all offline. The suite covers both provider regressions (via
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

### The scripts that cost money

Never run by CI; both `verify_*` scripts refuse to run when `CI` is set.

```bash
corpus run --x <handle> --max-posts 50 --budget 0.15 --skip-synthesis --capture-raw captures/
python scripts/verify_contract.py            # ~$0.01, monthly X drift check
python scripts/verify_contract.py --dry-run  # free, prints the plan

python scripts/verify_search_contract.py --target KEY --capture-search captures/
python scripts/verify_search_contract.py --target KEY --dry-run   # free
```

`verify_search_contract.py` answers the two questions the offline suite cannot:
whether `web_search_20250305` returns the shape the fixture assumes, and how
many candidates actually come back `corroborated` versus `held` — with a
breakdown of *why* each held one was held, and how many corroboration points
each one sat at. That last table is the calibration data for the 2.0-point
bar.

It guards that census at **both** ends. Before spending anything it probes page
reachability and refuses to run if fetches are blocked. Afterwards it checks
what the run actually did, and refuses to print a census when no candidate page
was read — or when the phase declined to promote anything for its own reasons.
A candidate whose page was never read has never had its strong signals looked
at, so the outcome mix measures the fetcher rather than the threshold, and it
looks exactly like evidence that the scorer is too strict.

The second guard exists because the first one is not sufficient and a live run
proved it: egress was fine, `example.com` answered, and the phase still read
zero of 50 candidates because it stopped before the verification pass. The
script printed a full census of it. "Can this machine fetch a page" and "did
this run read one" are different questions, and only the second one makes a
census mean anything.

> **Fixture provenance, search.** `tests/fixtures/web_search_response.json` is
> **real** — one of nine responses captured on 2026-08-03 with
> `--capture-search`, scrubbed by `tests/fixtures/_scrub_search.py`, which
> records exactly what was swapped and what was kept. The load-bearing finding
> in it is that `citations` came back **null in all nine**, so every search
> result's snippet is empty: a `web_search_result` has no snippet field, the
> snippet is built from the model's citations, and `SEARCH_SYSTEM` tells the
> model to reply with the single word "done". The snippet path is therefore
> unreachable in production today, and `web_search_response_with_citations.json`
> is written to the documented shape and labelled SYNTHETIC so the code that
> reads a citation still has something to run against.

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

### Both discovery phases are built; here is what they still cannot do

Phase 1 landed first because it costs nothing and may cover most targets, and
that was worth finding out before adding a search bill. Phase 2 followed. What
is still missing or unproven:

- **Commit messages, in practice.** `sources/github.py` extracts and filters
  them, but GitHub's events feed no longer carries a `commits` array — see the
  finding above — so in practice a run gets comments and no commits. Reading
  `/repos/{owner}/{repo}/commits?author={login}` per repo would recover them and
  is not built; there is no capture of that endpoint to build against.
- **The one unverified hop.** `github_issue_comments` is reached from a search
  result's `comments_url` and no capture of that response exists. The element
  shape is not a guess — it is the same comment object embedded in
  `IssueCommentEvent`, which *was* captured — but that the path returns a bare
  array of them is assumed. Declared unverified in the contract and pinned by a
  test.
- **The search *response* shape is now confirmed; the search *snippet* is
  confirmed absent.** Nine live responses on 2026-08-03 matched
  `corpus/search/contract.py` on every critical and important field, and the
  fixture is rebuilt from one of them. What they also showed is that no
  response carries a citation, so every snippet is empty — see the fixture
  provenance above. The tool survives that only because a snippet was never
  allowed to promote anything; a richer vendor (`exa` returns real page text)
  is the stub to reach for if snippets ever need to carry weight.
- **The scoring thresholds are still judgement, not measurement.** 2.0
  corroboration points for `corroborated` (strong 2, moderate 1, weak 0.5),
  two conflicting hosts for an ambiguous name, 80% for source concentration: round numbers
  chosen for where the failure mode changes, like the corpus tiers, and the
  code says so rather than implying precision it does not have. The
  strong/moderate split inside `links_to_anchor` — furniture versus prose — is
  the same kind of judgement. **Nobody has yet seen what fraction of real
  search results clear the bar.** The 2026-08-03 run was supposed to
  produce that number and could not: it read zero of 50 candidate pages,
  because the common-name check ran before the fetches on evidence it did not
  have. That gate is fixed and the script now refuses to print a census taken
  over unread candidates — but the calibration number still needs a run from a
  machine with egress to candidate hosts. The 50 candidates are worth
  re-running: 23 were rejected on their URL alone and 27 needed a page read,
  six of them the subject's own blog posts.
- **The `about them, not by them` detector is heuristic.** Author metadata
  naming someone else is solid; the URL markers and the third-person-quotation
  count are judgement calls that will occasionally hold a real page. It errs
  toward holding, which is the direction this tool errs everywhere.
- **`transcripts.py` and `paste.py`.** No YouTube captions, no podcast
  transcripts, and no local-file source — which is also the intended path for
  LinkedIn content you copy by hand.
- **Cross-source tiering is stated, not enforced.** A corpus more than 80% one
  source is now counted in Python, injected into the reduce prompt as ground
  truth, and stated in the report's coverage block. It is deliberately *not* a
  rule that deletes axes: "fewer than 40 documents" is arithmetic, while "a
  GitHub-only corpus cannot speak to their politics" is a judgement about
  subject matter, and encoding that as enforcement would be the topical
  filtering this tool refuses everywhere else.
