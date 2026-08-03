"""The GitHub response shape this tool depends on, stated once, explicitly.

Same job as `corpus/x/contract.py`, and it reuses that module's primitives
rather than restating them: `Field`, `EndpointContract`, and `check_payload`
describe "what the code reads and how sure we are", which is not an X-specific
idea. The alternative was a second copy that drifts.

Everything here was derived from three payloads pulled from the live API with
an authenticated token on **2026-08-03**, which is why most of it is confirmed
rather than assumed. Two findings from that capture shaped the adapter more
than the rest, and both are the kind that would otherwise be silent:

**1. No PushEvent carries commit messages.** All 69 PushEvents in the capture
had the payload `{repository_id, push_id, ref, head, before}` — no `commits`,
and no `size`/`distinct_size` either, while carrying a `repository_id` that is
absent from the documentation. This was consistent across a very large repo
(50 pushes) and a small hobby repo (19), so it is not size-related truncation.
The documented payload has all three. The adapter reads `commits` when it is
there and reports the count it actually found, so "GitHub stopped sending
these" and "this person pushed nothing" stay distinguishable.

**2. `search/issues?q=commenter:X` returns other people's writing.** Each item
is the *pull request* X commented on, so `item.body` and `item.user` belong to
whoever opened it. In the capture, **0 of 20 items were authored by the
subject**. An adapter that ingested `item.body` would attribute twenty
strangers' PR descriptions to the target — the exact failure the attribution
model exists to prevent, arriving from inside a source rather than from search.
So the search response is a list of *pointers*: it says where to look, and the
comments themselves are fetched per thread and filtered by author.
"""

from __future__ import annotations

from typing import Any

from ..x.contract import (
    CONDITIONAL,
    CRITICAL,
    IMPORTANT,
    OPTIONAL,
    EndpointContract,
    Field,
    Violation,
    check_payload,
)

# GitHub's own documented ceilings on the events feed, and the reason the
# adapter cannot promise historical coverage. Not measured here — the capture
# is one page — but stated so the report can say what it does not know.
EVENTS_MAX_EVENTS = 300
EVENTS_MAX_PAGE_SIZE = 100
EVENTS_WINDOW_DAYS = 90

# Rate limits, as returned by GET /rate_limit with this session's token on
# 2026-08-03: core 15,000/hr (an app installation), search **30/min**. The
# search limit is the tight one and is per-minute, not per-hour, which is why
# the adapter issues exactly one search per run and never paginates it.
SEARCH_LIMIT_PER_MINUTE = 30
UNAUTHENTICATED_CORE_PER_HOUR = 60
AUTHENTICATED_CORE_PER_HOUR = 5_000

# The comment object, which appears in three places with the same shape: inside
# IssueCommentEvent, inside PullRequestReviewCommentEvent, and as the elements
# of GET /repos/{o}/{r}/issues/{n}/comments. The first two are confirmed from
# the capture; the third is the same object per GitHub's docs and is the one
# unverified hop in the adapter.
COMMENT_FIELDS: tuple[Field, ...] = (
    Field(
        ("body",),
        CRITICAL,
        "the comment text — the entire reason this adapter exists. Absent, the "
        "adapter produces zero documents and exits cleanly",
        confirmed=True,
    ),
    Field(
        ("user",),
        CRITICAL,
        "the comment's author. Used to keep only the subject's own comments out "
        "of a thread other people are also in; absent, every participant's "
        "words would be attributed to the subject",
        confirmed=True,
    ),
    Field(
        ("html_url",),
        IMPORTANT,
        "the citation link in report.md. Absent, findings cite nothing",
        confirmed=True,
    ),
    Field(
        ("created_at",),
        CRITICAL,
        "ISO-8601 with a Z suffix. Drives cadence, ordering, and the date on every citation",
        confirmed=True,
    ),
    Field(("id",), IMPORTANT, "stable document id, and the dedupe key", confirmed=True),
)


USER = EndpointContract(
    name="github_user",
    path="/users/{login}",
    params=(),
    verified="2026-08-03, live GET with an authenticated token; keys below observed.",
    items=(
        Field(("login",), CRITICAL, "the account this run is about", confirmed=True),
        Field(("html_url",), IMPORTANT, "profile URL", confirmed=True),
        Field(
            ("name",),
            OPTIONAL,
            "display name; null on plenty of accounts",
            confirmed=True,
            nullable=True,
        ),
        Field(
            ("bio",),
            OPTIONAL,
            "self-description; null in the capture",
            confirmed=True,
            nullable=True,
        ),
        Field(
            ("company",),
            OPTIONAL,
            "scored against the card's employer",
            confirmed=True,
            nullable=True,
        ),
        Field(
            ("location",),
            OPTIONAL,
            "scored against the card's location",
            confirmed=True,
            nullable=True,
        ),
        Field(
            ("blog",),
            OPTIONAL,
            "the declared personal site discovery follows",
            confirmed=True,
            nullable=True,
        ),
        Field(
            ("twitter_username",),
            OPTIONAL,
            "corroborates an x anchor",
            confirmed=True,
            nullable=True,
        ),
        Field(("public_repos",), OPTIONAL, "coverage context", confirmed=True),
        Field(("created_at",), OPTIONAL, "account age", confirmed=True),
    ),
    notes=(
        "Confirmed keys: login, id, node_id, avatar_url, gravatar_id, url, "
        "html_url, followers_url, following_url, gists_url, starred_url, "
        "subscriptions_url, organizations_url, repos_url, events_url, "
        "received_events_url, type, user_view_type, site_admin, name, company, "
        "blog, location, email, hireable, bio, twitter_username, public_repos, "
        "public_gists, followers, following, created_at, updated_at.",
        "`blog` is an empty string rather than null when unset.",
    ),
)


EVENTS_PUBLIC = EndpointContract(
    name="github_events_public",
    path="/users/{login}/events/public",
    params=("per_page", "page"),
    verified=(
        "2026-08-03, live GET ?per_page=100 with an authenticated token. 100 "
        "events spanning 2026-07-12 to 2026-08-02 across two repositories."
    ),
    # A bare JSON array at the root, not an envelope. "$" is the sentinel for
    # "the payload itself is the item list".
    array_locations=("$",),
    items=(
        Field(("type",), CRITICAL, "event discriminator; drives all extraction", confirmed=True),
        Field(("actor",), CRITICAL, "who acted — checked against the subject", confirmed=True),
        Field(("repo",), IMPORTANT, "{id, name, url}; name is 'owner/repo'", confirmed=True),
        Field(("payload",), CRITICAL, "the per-type body", confirmed=True),
        Field(
            ("created_at",),
            CRITICAL,
            "ISO-8601 Z. The only timestamp a commit has here — the push is "
            "dated, the commits inside it are not — so it drives ordering and "
            "cadence for every commit document",
            confirmed=True,
        ),
        Field(
            ("id",),
            OPTIONAL,
            "the event id. Not read: documents dedupe on the comment or commit "
            "id inside the payload, which is stable across the two paths that "
            "can reach the same comment",
            confirmed=True,
        ),
        Field(("public",), OPTIONAL, "always true on this endpoint", confirmed=True),
    ),
    notes=(
        "Observed event types and counts: PushEvent 69, IssueCommentEvent 15, "
        "PullRequestEvent 9, IssuesEvent 6, PullRequestReviewCommentEvent 1.",
        "actor keys: id, login, display_login, gravatar_id, url, avatar_url. "
        "`comment.user.login` equalled `actor.login` on all 16 comment events, "
        "which is what lets the adapter trust the actor as the author.",
        "PushEvent payload keys, on all 69: repository_id, push_id, ref, head, "
        "before. NO `commits`, NO `size`, NO `distinct_size` — see the module "
        "docstring. `repository_id` is undocumented.",
        "PullRequestEvent.payload.pull_request is trimmed to {base, head, id, "
        "number, url} — no title and no body, so the endpoint cannot supply PR "
        "descriptions even for PRs the subject opened.",
        f"GitHub caps this feed at ~{EVENTS_MAX_EVENTS} events and ~"
        f"{EVENTS_WINDOW_DAYS} days. Coverage is recent activity, never history.",
    ),
)


# The documented PushEvent payload, kept separate from what was observed so the
# difference stays visible instead of being averaged into one optimistic spec.
PUSH_COMMIT_FIELDS: tuple[Field, ...] = (
    Field(("message",), CRITICAL, "the commit message; the whole point", confirmed=False),
    Field(("sha",), IMPORTANT, "document id and the link target", confirmed=False),
    Field(("author",), OPTIONAL, "{name, email}; not the GitHub login", confirmed=False),
    Field(("distinct",), OPTIONAL, "false for commits already pushed elsewhere", confirmed=False),
)


SEARCH_ISSUES_COMMENTER = EndpointContract(
    name="github_search_issues_commenter",
    path="/search/issues",
    params=("q", "per_page", "page", "advanced_search"),
    verified=(
        "2026-08-03, live GET ?q=commenter:{login}+type:pr with an "
        "authenticated token. 20 items of total_count 179."
    ),
    envelope=(
        Field(("total_count",), IMPORTANT, "how much was left unread", confirmed=True),
        Field(("incomplete_results",), OPTIONAL, "true when the search timed out", confirmed=True),
        Field(("items",), CRITICAL, "the pull requests, not the comments", confirmed=True),
    ),
    array_locations=("items",),
    items=(
        Field(
            ("comments_url",),
            CRITICAL,
            "where the subject's actual comments live. This is the only field "
            "on the item that leads to the subject's own words",
            confirmed=True,
        ),
        Field(
            ("html_url",), IMPORTANT, "the thread, linked as the comment's context", confirmed=True
        ),
        Field(
            ("repository_url",),
            IMPORTANT,
            "the repo the PR is in; the owner/repo tail becomes the context label",
            confirmed=True,
        ),
        Field(
            ("title",),
            IMPORTANT,
            "what the thread is about. Without it a comment arrives with no "
            "context at all, which for a reply is most of its meaning",
            confirmed=True,
        ),
        Field(("number",), OPTIONAL, "PR number. Not read; the URLs carry it", confirmed=True),
        Field(
            ("user",),
            OPTIONAL,
            "the PR AUTHOR, who is usually not the subject. Declared and "
            "deliberately NOT read: reading `body` and checking `user` first "
            "would work, and the adapter instead never reads `body` at all, "
            "which cannot be got wrong later",
            confirmed=True,
        ),
        Field(
            ("created_at",),
            OPTIONAL,
            "when the PR was opened. Not read — a document is dated by the "
            "comment inside it, not by the thread it sits in",
            confirmed=True,
        ),
        Field(
            ("pull_request",),
            OPTIONAL,
            "present only on PRs, which is how a PR is told from an issue",
            confirmed=True,
            presence=CONDITIONAL,
        ),
    ),
    notes=(
        "0 of 20 items were authored by the subject. `item.body` is the PR "
        "description, written by whoever opened it. Never a subject document.",
        f"Search has its own rate limit of {SEARCH_LIMIT_PER_MINUTE} requests "
        "per MINUTE, separate from and far tighter than the core limit. The "
        "adapter issues one search per run and does not paginate it.",
        "Reaches back further than events/public: the capture spans "
        "2026-01-12 to 2026-07-26 where the events feed started 2026-07-12.",
    ),
)


ISSUE_COMMENTS = EndpointContract(
    name="github_issue_comments",
    path="/repos/{owner}/{repo}/issues/{number}/comments",
    params=("per_page",),
    # The one hop with no capture behind it.
    verified="",
    array_locations=("$",),
    items=COMMENT_FIELDS,
    notes=(
        "UNVERIFIED as an endpoint. The element shape is not a guess, though: "
        "it is the same comment object embedded in IssueCommentEvent, which was "
        "confirmed on 2026-08-03. What is unconfirmed is that this path returns "
        "a bare array of them.",
        "Reached only from a search result's `comments_url`, never constructed "
        "by string formatting, so a change in GitHub's URL layout cannot send "
        "the adapter somewhere unintended.",
    ),
)


CONTRACTS: dict[str, EndpointContract] = {
    USER.name: USER,
    EVENTS_PUBLIC.name: EVENTS_PUBLIC,
    SEARCH_ISSUES_COMMENTER.name: SEARCH_ISSUES_COMMENTER,
    ISSUE_COMMENTS.name: ISSUE_COMMENTS,
}


def check(name: str, payload: Any, *, sample_items: int = 5) -> list[Violation]:
    """Check one payload against the named contract."""
    return check_payload(CONTRACTS[name], payload, sample_items=sample_items)


def unverified_endpoints() -> list[str]:
    return sorted(name for name, c in CONTRACTS.items() if not c.is_verified)
