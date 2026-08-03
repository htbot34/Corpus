"""The web-search response shape this tool depends on, stated once, explicitly.

Same job as `corpus/x/contract.py` and `corpus/sources/github_contract.py`, and
it reuses the first one's primitives rather than restating them: `Field`,
`EndpointContract`, and `check_payload` describe "what the code reads and how
sure we are", which is not an X-specific idea.

Why this is worth a file
------------------------
`results_from_message` reads the response the same tolerant way `_tweets_from`
reads a tweet page — it walks `content` looking for block types it recognises
and ignores everything else. That tolerance is right, because a Messages
response legitimately carries block types this code has no opinion about. It is
also exactly what turns a breaking change into a *silent* one: if
`web_search_tool_result` were renamed tomorrow, every search would return zero
results, every candidate would be held, and the run would exit 0 reporting that
the person has no web presence.

That failure is worse here than on the X side. A zero-result X ingest is
obvious — the report has no documents. A zero-result *search* looks exactly
like a target whose writing the anchors already cover, which is a normal and
expected outcome. Nothing in the output would look wrong.

Verification status
-------------------
**Everything here is `verified=""` — assumed from documentation, not observed
on the wire.** The shapes come from platform.claude.com's web-search tool page
(read 2026-08-03), and `tests/fixtures/web_search_response.json` was written
from the same reading, so the offline test proves the parser and the fixture
agree and proves nothing at all about the API.

`scripts/verify_search_contract.py` is the other half, and it has not been run:
this environment has no `ANTHROPIC_API_KEY` and its egress policy denies
CONNECT to every candidate host, so neither the search call nor the page fetch
behind it can be made. Stated here rather than in a commit message, because a
contract that quietly asserts unverified shapes is the thing this file exists
to prevent.
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
)

# Block types inside `content`. These are the strings `results_from_message`
# switches on, and a rename to any of them is invisible to it.
BLOCK_SERVER_TOOL_USE = "server_tool_use"
BLOCK_TOOL_RESULT = "web_search_tool_result"
BLOCK_RESULT = "web_search_result"
BLOCK_ERROR = "web_search_tool_result_error"
BLOCK_CITATION = "web_search_result_location"

# Documented error codes. Not exhaustive by contract — an unknown code is
# reported verbatim rather than swallowed — but a code that stops appearing
# while searches keep failing is worth noticing.
ERROR_CODES = (
    "too_many_requests",
    "invalid_tool_input",
    "max_uses_exceeded",
    "query_too_long",
    "request_too_large",
    "unavailable",
)


SEARCH_RESULT_FIELDS: tuple[Field, ...] = (
    Field(
        ("url",),
        CRITICAL,
        "the only part of a result the tool acts on; without it there is no "
        "candidate at all and the search silently returns nothing",
    ),
    Field(
        ("title",),
        IMPORTANT,
        "shown in unconfirmed.md, where a human decides from it. Absent, every "
        "held candidate is a bare URL and the decision gets harder",
    ),
    Field(
        ("page_age",),
        OPTIONAL,
        "becomes published_at when it parses. Unreadable values are left as "
        "None rather than guessed, so a change here degrades ordering only",
        presence=CONDITIONAL,
        nullable=True,
    ),
    Field(
        ("encrypted_content",),
        OPTIONAL,
        "deliberately dropped on the way to discovery.json — opaque, "
        "kilobytes per result, and only meaningful replayed to the same API",
        presence=CONDITIONAL,
    ),
)

CITATION_FIELDS: tuple[Field, ...] = (
    Field(
        ("url",),
        IMPORTANT,
        "joins a citation to the result it quotes. Without it the snippet "
        "cannot be attached to anything and every candidate loses its preview",
    ),
    Field(
        ("cited_text",),
        IMPORTANT,
        "the snippet itself, and the only readable text a search result ever "
        "carries — there is no snippet field on a web_search_result",
    ),
)

USAGE_FIELDS: tuple[Field, ...] = (
    Field(
        ("input_tokens",),
        IMPORTANT,
        "billed tokens; a search call's results arrive as input tokens and are "
        "the bulk of what it costs beyond the per-search fee",
    ),
    Field(
        ("output_tokens",),
        IMPORTANT,
        "billed tokens",
    ),
    Field(
        ("server_tool_use",),
        CRITICAL,
        "carries web_search_requests, which is the billing unit. If this stops "
        "arriving the run bills $0.00 for searches it actually made, and the "
        "budget stops being a ceiling",
    ),
)


WEB_SEARCH = EndpointContract(
    name="messages.create(tools=[web_search_20250305])",
    path="POST /v1/messages",
    params=("model", "max_tokens", "system", "tools", "messages"),
    verified="",  # see the module docstring: assumed, never observed here
    envelope=(
        Field(
            ("content",),
            CRITICAL,
            "the block list every result is read out of",
        ),
        Field(
            ("usage",),
            CRITICAL,
            "billing; without it a search is free as far as the budget knows",
        ),
        Field(
            ("stop_reason",),
            OPTIONAL,
            "`pause_turn` means the server paused a long search turn. With "
            "max_uses=1 it should not arrive; if it starts arriving, the "
            "single-search assumption behind the reservation is wrong",
            presence=CONDITIONAL,
        ),
    ),
    notes=(
        "An errored search is a 200 whose `content` is a single error object "
        "rather than a list. A caller that only catches exceptions sees "
        "silence; results_from_message returns the code instead.",
        "A search that matched nothing returns an empty list. That is a "
        "finding, not a failure, and must stay distinguishable from an error.",
        "max_uses=1 is pinned so one query is one billable search, which is "
        "what makes the pre-flight reservation exact.",
        "Citations are the only source of snippet text, and per the docs are not billed as tokens.",
    ),
)


# --------------------------------------------------------------------------
# checking a real response
# --------------------------------------------------------------------------


def _blocks(payload: Any, kind: str) -> list[dict[str, Any]]:
    content = (payload or {}).get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind]


def check_search_response(payload: Any) -> list[Violation]:
    """Check one Messages response against the contract above.

    Used by both halves: the offline fixture test and the live checker. One
    spec, two checkers — a checklist kept in two places is a checklist that
    disagrees with itself inside a month.
    """
    name = WEB_SEARCH.name
    violations: list[Violation] = []

    if not isinstance(payload, dict):
        return [Violation(name, CRITICAL, f"response is {type(payload).__name__}, not an object")]

    for field in WEB_SEARCH.envelope:
        if field.find(payload) is None and field.presence != CONDITIONAL:
            violations.append(
                Violation(
                    name,
                    field.severity,
                    f"envelope field {field.primary!r} is absent — {field.why}",
                )
            )

    # The block the whole phase depends on. Its absence is the silent failure
    # this file exists to catch: zero results reads as "no web presence".
    tool_results = _blocks(payload, BLOCK_TOOL_RESULT)
    if not tool_results:
        violations.append(
            Violation(
                name,
                CRITICAL,
                f"no {BLOCK_TOOL_RESULT!r} block in the response. Either the model did not "
                f"search, or the block was renamed — and a rename makes every search return "
                f"nothing while the run exits 0 reporting no web presence",
            )
        )
        return violations

    if not _blocks(payload, BLOCK_SERVER_TOOL_USE):
        violations.append(
            Violation(
                name,
                OPTIONAL,
                f"no {BLOCK_SERVER_TOOL_USE!r} block; the query that ran is not recoverable "
                f"from the response, which only costs capture readability",
            )
        )

    saw_results = False
    for block in tool_results:
        content = block.get("content")
        if isinstance(content, dict):
            code = content.get("error_code")
            if code and code not in ERROR_CODES:
                violations.append(Violation(name, OPTIONAL, f"undocumented error_code {code!r}"))
            continue
        if not isinstance(content, list):
            violations.append(
                Violation(
                    name,
                    CRITICAL,
                    f"{BLOCK_TOOL_RESULT}.content is {type(content).__name__}; expected a "
                    f"list of results or a single error object",
                )
            )
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != BLOCK_RESULT:
                continue
            saw_results = True
            for field in SEARCH_RESULT_FIELDS:
                if field.presence == CONDITIONAL:
                    continue
                if field.find(item) is None:
                    violations.append(
                        Violation(
                            name,
                            field.severity,
                            f"{BLOCK_RESULT}.{field.primary} is absent — {field.why}",
                        )
                    )

    usage = payload.get("usage")
    if isinstance(usage, dict):
        for field in USAGE_FIELDS:
            if field.find(usage) is None:
                violations.append(
                    Violation(
                        name, field.severity, f"usage.{field.primary} is absent — {field.why}"
                    )
                )
        server = usage.get("server_tool_use")
        if isinstance(server, dict) and "web_search_requests" not in server:
            violations.append(
                Violation(
                    name,
                    CRITICAL,
                    "usage.server_tool_use.web_search_requests is absent — this is the "
                    "billing unit, and without it searches are billed as free",
                )
            )

    # Citations only exist when the model quoted something, so their absence is
    # reported once per payload rather than per result.
    if saw_results:
        cited = [
            c
            for block in _blocks(payload, "text")
            for c in (block.get("citations") or [])
            if isinstance(c, dict)
        ]
        if not cited:
            violations.append(
                Violation(
                    name,
                    OPTIONAL,
                    "no citations in a response that returned results, so every candidate "
                    "has an empty snippet. Held candidates then reach unconfirmed.md with "
                    "no preview text for a human to judge from",
                )
            )
        for citation in cited:
            for field in CITATION_FIELDS:
                if field.find(citation) is None:
                    violations.append(
                        Violation(
                            name,
                            field.severity,
                            f"{BLOCK_CITATION}.{field.primary} is absent — {field.why}",
                        )
                    )
            break  # one representative citation is enough to catch a rename

    return violations


#: Every field name the parser reads, for the cross-check in the other
#: direction: a field the contract describes that no code reads is a contract
#: that has drifted from the tool.
FIELDS_THE_PARSER_READS: frozenset[str] = frozenset(
    {
        "content",
        "type",
        "usage",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "server_tool_use",
        "web_search_requests",
        "url",
        "title",
        "page_age",
        "citations",
        "cited_text",
        "error_code",
        "stop_reason",
    }
)
