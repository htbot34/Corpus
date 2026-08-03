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
**Observed on the wire on 2026-08-03**, across nine live responses from
`scripts/verify_search_contract.py --target dustinw --capture-search`. The
fixture is one of those nine, scrubbed — see `tests/fixtures/_scrub_search.py`.
Every field below marked `confirmed` was read off a real payload rather than
off the documentation, and the documented shapes held: `content`, `usage`,
`web_search_tool_result`, and `url`/`title` on every one of 68 results.

Four things the docs did not say, all of them observed:

* **No response carried a citation.** All nine came back `citations: null`, so
  every result's snippet was empty. That is not drift — it is what this tool's
  own `SEARCH_SYSTEM` produces, since it tells the model to reply with the
  single word "done" and a model that writes nothing quotes nothing. Snippets
  are therefore structurally absent in production, which is survivable only
  because a snippet was never allowed to promote anything; see
  `corpus/search/verify.py` for what it *was* allowed to do, and what that cost.
* **Blocks carry an undocumented `caller`** — `null` on `server_tool_use`,
  `{"type": "direct"}` on `web_search_tool_result`.
* **`usage` carries more than the docs list**: `cache_creation`,
  `inference_geo`, `output_tokens_details`, `service_tier`, and
  `server_tool_use.web_fetch_requests` alongside `web_search_requests`. All
  additive; nothing the parser reads moved.
* **`page_age` is sometimes relative prose** ("1 month ago"), not only an
  absolute date. `parse_page_age` returns None for it, which is the intended
  behaviour: a guessed date silently reorders a corpus.
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
        confirmed=True,
    ),
    Field(
        ("title",),
        IMPORTANT,
        "shown in unconfirmed.md, where a human decides from it. Absent, every "
        "held candidate is a bare URL and the decision gets harder. With no "
        "snippet ever arriving, it is also the only text a candidate has",
        confirmed=True,
    ),
    Field(
        ("page_age",),
        OPTIONAL,
        "becomes published_at when it parses. Present-but-null on 53 of 68 "
        "observed results, and sometimes relative prose ('1 month ago') rather "
        "than a date; unreadable values are left as None rather than guessed",
        confirmed=True,
        presence=CONDITIONAL,
        nullable=True,
    ),
    Field(
        ("encrypted_content",),
        OPTIONAL,
        "deliberately dropped on the way to discovery.json — opaque, "
        "272 to 1,932 bytes per result observed, and only meaningful replayed "
        "to the same API",
        confirmed=True,
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
        "carries — there is no snippet field on a web_search_result. Never "
        "observed: see this module's docstring on why none arrive",
    ),
)

USAGE_FIELDS: tuple[Field, ...] = (
    Field(
        ("input_tokens",),
        IMPORTANT,
        "billed tokens; a search call's results arrive as input tokens and are "
        "the bulk of what it costs beyond the per-search fee",
        confirmed=True,
    ),
    Field(
        ("output_tokens",),
        IMPORTANT,
        "billed tokens",
        confirmed=True,
    ),
    Field(
        ("server_tool_use",),
        CRITICAL,
        "carries web_search_requests, which is the billing unit. If this stops "
        "arriving the run bills $0.00 for searches it actually made, and the "
        "budget stops being a ceiling",
        confirmed=True,
    ),
)


WEB_SEARCH = EndpointContract(
    name="messages.create(tools=[web_search_20250305])",
    path="POST /v1/messages",
    params=("model", "max_tokens", "system", "tools", "messages"),
    verified=(
        "2026-08-03, nine live responses (68 results) captured by "
        "scripts/verify_search_contract.py --target dustinw --capture-search"
    ),
    envelope=(
        Field(
            ("content",),
            CRITICAL,
            "the block list every result is read out of",
            confirmed=True,
        ),
        Field(
            ("usage",),
            CRITICAL,
            "billing; without it a search is free as far as the budget knows",
            confirmed=True,
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
        "what makes the pre-flight reservation exact. Observed exactly 1 per "
        "call across nine calls.",
        "Citations are the only source of snippet text, and per the docs are not billed as tokens. "
        "None arrived in nine live responses — SEARCH_SYSTEM tells the model to write one word "
        "— so in practice every snippet is empty and nothing may depend on one.",
        "A single web_search_tool_result block carried 6 to 10 results; RESULTS_PER_QUERY "
        "truncates to 8, so the tail of a broad query is dropped by us, not by the API.",
        "Blocks carry an undocumented `caller`, and `usage` carries five keys the docs do "
        "not list. Both additive, neither read.",
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

    # A response with no citations is the *observed norm*, not drift: nine of
    # nine came back `citations: null`, because the search system prompt tells
    # the model to write one word. Reporting the normal case as a violation on
    # every single run is how a report gets ignored — the same reasoning that
    # keeps a rate-limited search off this list. What is still worth catching
    # is a citation that arrives with its fields renamed, so the shape is
    # checked whenever one is actually present.
    if saw_results:
        cited = [
            c
            for block in _blocks(payload, "text")
            for c in (block.get("citations") or [])
            if isinstance(c, dict)
        ]
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
