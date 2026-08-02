"""The provider response shape this tool depends on, stated once, explicitly.

Why this file exists
--------------------
Every field the code reads is read through a fallback chain — ``_first(raw,
"createdAt", "created_at", "timestamp")``, ``_tweets_from`` probing four
locations. That tolerance is good engineering against a provider that has moved
its shapes before, and it is also exactly what turns a breaking provider change
into a *silent* one: when every lookup has a default, a renamed field produces
zero posts and a cheerful exit code rather than an error.

So the tolerant readers stay, and this file states what they are tolerating.
Two consumers check against it:

* ``tests/test_wire_contract.py`` — offline, against the fixtures, on every run.
* ``scripts/verify_contract.py`` — online, against the live API, run by hand
  for about a cent when provider drift is suspected.

One spec, two checkers. A checklist maintained in two places is a checklist
that disagrees with itself within a month.

Verification status
-------------------
``EndpointContract.verified`` records whether a shape was *observed on the wire*
or merely *read in documentation*. That distinction is the entire point of the
exercise, so it is a required field rather than a comment: an unverified
contract asserts what the code assumes, which is useful, but it must never be
mistaken for evidence about the provider.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Severity drives how a violation is reported, not whether it is reported.
#
#   critical  — absent, the run silently produces zero or corrupt results.
#   important — absent, output quality degrades in a way a reader would notice.
#   optional  — absent, a nice-to-have is missing and nothing breaks.
CRITICAL = "critical"
IMPORTANT = "important"
OPTIONAL = "optional"

# Presence: whether a field is expected on every item, or only on the items it
# applies to. `quoted_tweet` is absent from a tweet that quotes nothing, and
# asserting it per-item would mean every ordinary post "violates" the contract —
# noise that trains a reader to ignore the report.
#
# Conditional fields are still worth checking, just at a different scale: never
# seeing `inReplyToId` anywhere across a 50-post capture that certainly contains
# replies is exactly the renamed-field signal this file exists to catch. So they
# are checked per *payload* and reported as "never observed" rather than per item.
ALWAYS = "always"
CONDITIONAL = "conditional"


@dataclass(frozen=True)
class Field:
    """One value the code reads, and every spelling it will accept."""

    names: tuple[str, ...]
    severity: str
    why: str
    confirmed: bool = False  # observed on the wire, as opposed to assumed
    presence: str = ALWAYS

    @property
    def primary(self) -> str:
        return self.names[0]

    def find(self, obj: dict[str, Any]) -> tuple[str, Any] | None:
        for name in self.names:
            if name in obj and obj[name] is not None:
                return name, obj[name]
        return None


@dataclass(frozen=True)
class EndpointContract:
    name: str
    path: str
    params: tuple[str, ...]
    verified: str  # "" when unverified; otherwise a date and how it was checked
    envelope: tuple[Field, ...] = ()
    array_locations: tuple[str, ...] = ()  # dotted paths probed for the item list
    items: tuple[Field, ...] = ()
    cursor: Field | None = None
    has_more: Field | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_verified(self) -> bool:
        return bool(self.verified)


@dataclass
class Violation:
    endpoint: str
    severity: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.endpoint}: {self.message}"


# --------------------------------------------------------------------------
# Shared tweet-object fields
# --------------------------------------------------------------------------
# Read by normalize_tweet / _tweet_id / parse_created_at in client.py. The two
# criticals are not a judgement call:
#
#   id        — ingest.py drops any tweet without one, before dedupe, silently.
#   createdAt — drives window_earliest, and therefore the backwards walk itself.
#               A createdAt that stops parsing does not merely mislabel a post;
#               it makes the walk advance one second per window while paying for
#               every page. See Phase 2.6.

TWEET_FIELDS: tuple[Field, ...] = (
    Field(
        ("id", "id_str", "tweet_id"),
        CRITICAL,
        "tweet identity; without it ingest.py silently drops the tweet and "
        "dedupe across windows stops working",
    ),
    Field(
        ("createdAt", "created_at", "timestamp"),
        CRITICAL,
        "drives window_earliest and the backwards walk; a shape change here "
        "burns budget making no progress rather than failing",
    ),
    Field(
        ("text", "full_text", "fullText"),
        CRITICAL,
        "the corpus itself",
    ),
    Field(
        ("author", "user"),
        IMPORTANT,
        "author handle, used to distinguish self-replies (thread stitching) "
        "from replies to other people",
    ),
    Field(
        ("isReply", "is_reply", "inReplyToId", "in_reply_to_status_id_str"),
        IMPORTANT,
        "reply classification; without it every reply is misfiled as an "
        "original and parent hydration never fires",
    ),
    Field(
        ("inReplyToId", "in_reply_to_status_id_str", "in_reply_to_id"),
        IMPORTANT,
        "the parent id to hydrate — the field that makes a reply readable",
        presence=CONDITIONAL,
    ),
    Field(
        ("quoted_tweet", "quotedTweet"),
        IMPORTANT,
        "quote target; inline quotes cost nothing extra to hydrate",
        presence=CONDITIONAL,
    ),
    Field(
        ("retweeted_tweet", "retweetedTweet"),
        IMPORTANT,
        "repost detection; reposts are excluded from synthesis by default",
        presence=CONDITIONAL,
    ),
    Field(("url", "twitterUrl", "tweet_url"), IMPORTANT, "citation links in report.md"),
    Field(("entities",), IMPORTANT, "t.co expansion and outbound-domain signal"),
    Field(("likeCount", "favorite_count", "likes"), OPTIONAL, "engagement baseline"),
    Field(("replyCount", "reply_count", "replies"), OPTIONAL, "engagement baseline"),
    Field(("retweetCount", "retweet_count", "reposts"), OPTIONAL, "engagement baseline"),
    Field(("viewCount", "view_count", "views"), OPTIONAL, "engagement baseline"),
)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

USER_INFO = EndpointContract(
    name="user_info",
    path="/twitter/user/info",
    params=("userName",),
    verified=(
        "2026-07-31, live GET /twitter/user/info?userName=paulg. Envelope and "
        "data field names confirmed against the response body."
    ),
    envelope=(
        Field(("status",), IMPORTANT, "success/error discriminator", confirmed=True),
        Field(
            ("msg",),
            OPTIONAL,
            "human-readable status; present on the wire and absent from the "
            "original fixture, which is how the fixtures were caught being "
            "synthetic in the first place",
            confirmed=True,
        ),
        Field(("data",), CRITICAL, "the profile object", confirmed=True),
    ),
    items=(
        Field(("id",), CRITICAL, "numeric user id", confirmed=True),
        Field(("userName",), CRITICAL, "the handle", confirmed=True),
        Field(("name",), IMPORTANT, "display name", confirmed=True),
        Field(
            ("statusesCount",),
            CRITICAL,
            "public post count. Phase 2.3 reports ingested-vs-total with it, so "
            "a 400-of-53,901 run is obvious rather than buried in stop_reason",
            confirmed=True,
        ),
        Field(
            ("createdAt",),
            IMPORTANT,
            "account creation. Real format is ISO 8601 with six-digit "
            "microseconds and a Z suffix (2010-08-27T20:13:59.000000Z), NOT the "
            "legacy 'Mon Mar 03 12:00:00 +0000 2014' form the fixture assumed",
            confirmed=True,
        ),
        Field(("followers",), OPTIONAL, "reach context", confirmed=True),
        Field(("following",), OPTIONAL, "reach context", confirmed=True),
        Field(("description",), OPTIONAL, "bio", confirmed=True),
        Field(("isBlueVerified",), OPTIONAL, "verification state", confirmed=True),
    ),
    notes=(
        "Confirmed data keys: id, name, userName, location, url, description, "
        "entities, protected, isVerified, isBlueVerified, verifiedType, "
        "followers, following, favouritesCount, statusesCount, mediaCount, "
        "createdAt, coverPicture, profilePicture, canDm, "
        "affiliatesHighlightedLabel, isAutomated, automatedBy, pinnedTweetIds.",
        "Note 'favouritesCount' uses the British spelling.",
    ),
)

ADVANCED_SEARCH = EndpointContract(
    name="advanced_search",
    path="/twitter/tweet/advanced_search",
    params=("query", "queryType", "cursor"),
    verified="",  # UNVERIFIED — see docs/wire-contract.md
    envelope=(),
    array_locations=("tweets", "data.tweets", "data", "results"),
    items=TWEET_FIELDS,
    cursor=Field(
        ("next_cursor", "nextCursor"),
        CRITICAL,
        "within-window pagination; wrong name means one page per window and "
        "a quietly truncated history",
    ),
    has_more=Field(
        ("has_next_page", "hasNextPage"),
        CRITICAL,
        "end-of-window signal. _cursor_from falls back to bool(cursor), so a "
        "provider that always returns a cursor would loop to --max-pages every "
        "window and pay for it",
    ),
    notes=(
        "Uses since_time:/until_time: (unix seconds) inside the query string. "
        "The date-level since:/until: operators are not honoured by the "
        "underlying index. Whether the unix-second operators are honoured as "
        "documented is UNVERIFIED and is the single most expensive thing in "
        "this file to be wrong about.",
    ),
)

LAST_TWEETS = EndpointContract(
    name="last_tweets",
    path="/twitter/user/last_tweets",
    params=("userName", "cursor"),
    verified="",  # UNVERIFIED
    array_locations=("data.tweets", "tweets", "data", "results"),
    items=TWEET_FIELDS,
    cursor=Field(("next_cursor", "nextCursor"), CRITICAL, "timeline pagination"),
    has_more=Field(("has_next_page", "hasNextPage"), CRITICAL, "end-of-timeline signal"),
)

TWEETS_BY_IDS = EndpointContract(
    name="tweets_by_ids",
    path="/twitter/tweets",
    params=("tweet_ids",),
    verified="",  # UNVERIFIED
    array_locations=("tweets", "data.tweets", "data", "results"),
    items=TWEET_FIELDS,
    notes=(
        "tweet_ids is sent comma-joined. The 100-id ceiling in providers.py is "
        "from documentation, not measurement — UNVERIFIED. So is the response "
        "for a deleted or protected parent, which hydrate.py renders as "
        "'[unavailable]' on the assumption that it is simply absent from the "
        "returned array rather than present with an error marker.",
    ),
)

CONTRACTS: dict[str, EndpointContract] = {
    c.name: c for c in (USER_INFO, ADVANCED_SEARCH, LAST_TWEETS, TWEETS_BY_IDS)
}


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------


def resolve(payload: dict[str, Any], dotted: str) -> Any:
    """Walk a dotted path, returning None rather than raising on a miss."""
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def locate_items(
    contract: EndpointContract, payload: dict[str, Any]
) -> tuple[str | None, list[Any]]:
    """Find the item array, returning (where it was found, the items).

    Deliberately mirrors `_tweets_from`'s probe order so the checker agrees with
    the code rather than describing an idealized version of it.
    """
    for location in contract.array_locations:
        found = resolve(payload, location)
        if isinstance(found, list):
            return location, found
    return None, []


def check_payload(
    contract: EndpointContract,
    payload: dict[str, Any],
    *,
    sample_items: int = 5,
) -> list[Violation]:
    """Compare one real payload against the contract.

    Returns violations rather than raising, so a caller can report all of them
    at once. Finding out about one renamed field per run is how a migration
    takes six runs.
    """
    out: list[Violation] = []

    def note(severity: str, message: str) -> None:
        out.append(Violation(contract.name, severity, message))

    if not isinstance(payload, dict):
        note(CRITICAL, f"response is {type(payload).__name__}, expected a JSON object")
        return out

    for spec in contract.envelope:
        if spec.find(payload) is None:
            note(
                spec.severity,
                f"envelope field {spec.primary!r} missing "
                f"(accepted: {', '.join(spec.names)}) — {spec.why}. "
                f"Top-level keys present: {sorted(payload)}",
            )

    # user_info has no array; its "items" describe the single data object.
    if contract.array_locations:
        location, items = locate_items(contract, payload)
        if location is None:
            note(
                CRITICAL,
                "no item array found. _tweets_from probes "
                f"{list(contract.array_locations)} and would return [] here, "
                "which the pipeline cannot distinguish from an account with no "
                f"posts. Top-level keys present: {sorted(payload)}",
            )
            return out
        if location != contract.array_locations[0]:
            note(
                IMPORTANT,
                f"item array found at {location!r}, not the expected "
                f"{contract.array_locations[0]!r}. Still handled, but the "
                "contract doc is now out of date",
            )
        subjects: Iterable[Any] = items[:sample_items]
    else:
        data = resolve(payload, "data")
        subjects = [data if isinstance(data, dict) else payload]

    subjects = list(subjects)
    for index, item in enumerate(subjects):
        if not isinstance(item, dict):
            note(CRITICAL, f"item {index} is {type(item).__name__}, expected an object")
            continue
        for spec in contract.items:
            if spec.presence == CONDITIONAL:
                continue
            if spec.find(item) is None:
                note(
                    spec.severity,
                    f"item {index}: {spec.primary!r} missing "
                    f"(accepted: {', '.join(spec.names)}) — {spec.why}",
                )

    # Conditional fields, checked across the payload rather than per item.
    # Reported at OPTIONAL because one payload is weak evidence — a page of
    # three originals legitimately contains no reply parent. The live checker
    # aggregates across a whole capture, where "never seen" carries real weight.
    dict_items = [i for i in subjects if isinstance(i, dict)]
    if dict_items:
        for spec in contract.items:
            if spec.presence != CONDITIONAL:
                continue
            if not any(spec.find(i) for i in dict_items):
                note(
                    OPTIONAL,
                    f"{spec.primary!r} not observed on any of {len(dict_items)} "
                    f"sampled items (accepted: {', '.join(spec.names)}). Expected "
                    f"only where applicable, so this is a signal rather than a "
                    f"failure — but if the sample certainly contains one, the "
                    f"field has been renamed. {spec.why}",
                )

    if contract.cursor is not None or contract.has_more is not None:
        out.extend(_check_pagination(contract, payload))
    return out


def _check_pagination(contract: EndpointContract, payload: dict[str, Any]) -> list[Violation]:
    out: list[Violation] = []
    cursor_hit = contract.cursor.find(payload) if contract.cursor else None
    more_hit = contract.has_more.find(payload) if contract.has_more else None

    if contract.has_more is not None and more_hit is None:
        out.append(
            Violation(
                contract.name,
                contract.has_more.severity,
                f"{contract.has_more.primary!r} missing "
                f"(accepted: {', '.join(contract.has_more.names)}) — "
                f"{contract.has_more.why}. Present keys: {sorted(payload)}",
            )
        )
    if contract.cursor is not None and cursor_hit is None and more_hit and more_hit[1]:
        out.append(
            Violation(
                contract.name,
                CRITICAL,
                f"has-more is true but no cursor field found "
                f"(accepted: {', '.join(contract.cursor.names)}) — pagination "
                "would stop after page 1 with more data available",
            )
        )
    return out


def worst_severity(violations: list[Violation]) -> str | None:
    for level in (CRITICAL, IMPORTANT, OPTIONAL):
        if any(v.severity == level for v in violations):
            return level
    return None


def unverified_endpoints() -> list[str]:
    """Endpoints whose shape is assumed from documentation, not observed."""
    return sorted(name for name, c in CONTRACTS.items() if not c.is_verified)
