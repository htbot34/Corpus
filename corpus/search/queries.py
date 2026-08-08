"""What to search for, built from the identity card.

**Never search a bare name.** A name alone is the query most likely to return
somebody else, and it is the one query a person would type by hand — so it is
the one this module refuses to produce. Every query below pairs the name with
something that narrows it: an employer, a role, a handle, or the vocabulary of
published writing.

A query whose required field is absent is skipped rather than emitted with an
empty slot. `"Jane Smith" ""` is not a narrower search than `"Jane Smith"`; it
is the same search with a syntax error in it.

Order is precision first, because `--max-searches` truncates from the end. The
employer and handle queries are the ones that come back with the right person,
so they run whether the cap is 12 or 3; the conference and podcast sweeps are
the ones worth losing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..identity import IdentityCard

# Twelve queries at ~$0.01 of search fee each is ~$0.12, which sits inside the
# $1.50-$3.00 per-target budget the README quotes. Raise it for a target with a
# common name and a thin corpus; the cache means a re-run at a higher cap only
# pays for the queries it has not already run.
DEFAULT_MAX_SEARCHES = 12

# Search operators are meaningful, and a name is user input. Anything that
# could change what the query *means* — quotes, boolean operators, field
# prefixes — is stripped rather than escaped, because there is no escaping
# convention shared across search vendors.
_UNSAFE = re.compile(r'["()\[\]{}<>|]|(?<!\w)(?:OR|AND|NOT)(?!\w)|\b\w+:')


def sanitize(value: str) -> str:
    """A card field, made safe to interpolate into a query."""
    return " ".join(_UNSAFE.sub(" ", value or "").split()).strip()


def quoted(value: str) -> str:
    clean = sanitize(value)
    return f'"{clean}"' if clean else ""


@dataclass(frozen=True)
class Query:
    """One search, and what it is for.

    `why` is not decoration: it lands in `unconfirmed.md` next to every
    candidate the query produced, and "found by a search for their employer" is
    what lets a human judge a URL in two seconds instead of twenty.
    """

    text: str
    why: str
    #: Card fields this query interpolates. Used only for reporting which
    #: missing field cost which query.
    uses: tuple[str, ...] = ()


def generate_queries(
    card: IdentityCard,
    max_searches: int = DEFAULT_MAX_SEARCHES,
) -> list[Query]:
    """Queries for this target, most precise first, capped at `max_searches`."""
    name = quoted(card.name)
    employer = quoted(card.employer)
    role = quoted(card.role)
    location = sanitize(card.location)
    handle = sanitize(card.x_handle)
    github = sanitize(card.anchors.get("github", ""))

    queries: list[Query] = []

    def add(text: str, why: str, *uses: str) -> None:
        text = " ".join(text.split())
        if text and all(q.text != text for q in queries):
            queries.append(Query(text=text, why=why, uses=uses))

    # The cheapest disambiguator there is, so it goes on the two queries that
    # carry the most weight — the employer pairing and the writing sweep.
    # Everywhere else it costs recall without buying much precision: a person's
    # conference talks are not tagged with where they live.
    def with_location(text: str) -> str:
        return f"{text} {location}" if location else text

    if name and employer:
        add(
            with_location(f"{name} {employer}"),
            "name and employer together",
            "name",
            "employer",
        )
    if handle:
        # A handle is not a name: people cite each other by handle, and a
        # handle is unique in a way "Jane Smith" is not.
        add(f'"@{handle}"', "their X handle, as other people write it", "x")
    if github:
        add(f"{github} github", "their GitHub username", "github")
    bluesky = sanitize(card.anchors.get("bluesky", ""))
    if bluesky:
        add(f'"{bluesky}"', "their Bluesky handle, as other people cite it", "bluesky")
    if name and role:
        add(f"{name} {role}", "name and role together", "name", "role")
    if name:
        add(
            with_location(f"{name} blog OR essay OR writing"),
            "the vocabulary of published writing",
            "name",
        )
        add(f"{name} author OR byline", "bylines", "name")
        add(
            f"{name} interview OR podcast OR transcript",
            "interviews and transcripts",
            "name",
        )
        add(f"{name} talk OR keynote OR conference", "talks", "name")
    if name and employer:
        add(
            f"{name} {employer} site:news.ycombinator.com",
            "Hacker News, where technical people argue under their own name",
            "name",
            "employer",
        )

    return queries[: max(0, max_searches)]


def missing_fields(card: IdentityCard) -> list[str]:
    """Card fields that would buy more queries, most useful first.

    Named in this order because that is the order they disambiguate in:
    employer separates two people with the same name almost always, location
    often, role sometimes.
    """
    return [
        label
        for label in ("employer", "location", "role")
        if not str(getattr(card, label, "") or "").strip()
    ]
