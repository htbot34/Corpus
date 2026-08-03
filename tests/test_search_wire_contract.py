"""The offline half of the search wire contract.

`corpus/search/contract.py` states what `results_from_message` depends on;
this checks the fixture against it on every test run, and
`scripts/verify_search_contract.py` checks the live API against the same spec.
One spec, two checkers.

What this can and cannot prove, stated plainly because the distinction is the
entire point: the fixture was written from the same documentation the contract
was, so agreement here proves the parser and the fixture are consistent. It
proves nothing about the API. `WEB_SEARCH.verified` is empty and a test below
asserts that it stays empty until someone runs the live checker and records
what they saw.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpus.search.contract import (
    BLOCK_RESULT,
    BLOCK_TOOL_RESULT,
    CITATION_FIELDS,
    FIELDS_THE_PARSER_READS,
    SEARCH_RESULT_FIELDS,
    USAGE_FIELDS,
    WEB_SEARCH,
    check_search_response,
)
from corpus.search.providers import results_from_message, usage_from_message
from corpus.x.contract import CRITICAL, worst_severity

FIXTURE = Path(__file__).parent / "fixtures" / "web_search_response.json"


def response() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def severities(payload: Any) -> set[str]:
    return {v.severity for v in check_search_response(payload)}


# -- the fixture satisfies the contract -------------------------------------


def test_the_fixture_satisfies_the_contract() -> None:
    violations = check_search_response(response())
    assert violations == [], "\n".join(str(v) for v in violations)


def test_the_contract_is_honest_about_never_having_been_verified() -> None:
    """The one assertion that must fail loudly the day someone runs the live
    checker: `verified` is set from what they observed, and this test is how
    they are reminded to record it."""
    assert WEB_SEARCH.verified == "", (
        "the contract now claims verification — update this test with what was "
        "observed, and say so in the fixture's _provenance too"
    )
    assert not WEB_SEARCH.is_verified


def test_the_fixture_says_it_was_written_from_documentation() -> None:
    payload = response()
    assert "NOT captured from the wire" in payload["_provenance"]


# -- the shapes that would fail silently ------------------------------------


def test_a_renamed_result_block_is_caught() -> None:
    """The silent failure this file exists for. A zero-result search looks
    exactly like a target the anchors already cover — nothing in the report
    would look wrong."""
    payload = response()
    payload["content"][2]["type"] = "web_search_tool_result_v2"

    violations = check_search_response(payload)

    assert worst_severity(violations) == CRITICAL
    assert any(BLOCK_TOOL_RESULT in str(v) for v in violations)
    # And the parser really does go quiet, which is why it is critical.
    assert results_from_message(payload, limit=10)[0] == []


def test_a_result_without_a_url_is_critical() -> None:
    payload = response()
    del payload["content"][2]["content"][0]["url"]

    assert CRITICAL in severities(payload)


def test_losing_the_billing_unit_is_critical() -> None:
    """Without `web_search_requests` the run bills $0.00 for searches it made,
    and `--budget` stops being a ceiling."""
    payload = response()
    del payload["usage"]["server_tool_use"]

    violations = check_search_response(payload)

    assert worst_severity(violations) == CRITICAL
    assert usage_from_message(payload, "m").searches == 0


def test_a_renamed_requests_counter_is_caught() -> None:
    payload = response()
    payload["usage"]["server_tool_use"] = {"web_search_calls": 1}

    assert CRITICAL in severities(payload)


def test_a_result_list_that_becomes_something_else_is_caught() -> None:
    payload = response()
    payload["content"][2]["content"] = "two results"

    assert CRITICAL in severities(payload)


# -- shapes that are fine, and must not cry wolf ----------------------------


def test_an_errored_search_is_not_a_contract_violation() -> None:
    """A rate limit is the API working. Reporting it as drift would train a
    reader to ignore the report."""
    payload = response()
    payload["content"][2]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "too_many_requests",
    }

    assert check_search_response(payload) == []


def test_an_undocumented_error_code_is_noted_but_not_fatal() -> None:
    payload = response()
    payload["content"][2]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "brand_new_code",
    }

    violations = check_search_response(payload)

    assert violations and worst_severity(violations) != CRITICAL


def test_a_result_with_no_page_age_is_fine() -> None:
    payload = response()
    for item in payload["content"][2]["content"]:
        item.pop("page_age", None)

    assert check_search_response(payload) == []


def test_a_response_with_no_citations_is_noticed_but_not_fatal() -> None:
    """Every candidate reaches unconfirmed.md with no preview text, which makes
    a human's job harder without making the tool wrong."""
    payload = response()
    for block in payload["content"]:
        block.pop("citations", None)

    violations = check_search_response(payload)

    assert violations and worst_severity(violations) != CRITICAL
    assert any("snippet" in str(v) for v in violations)


# -- the cross-check, in the other direction --------------------------------


def test_every_field_the_contract_describes_is_one_the_parser_reads() -> None:
    """The direction that caught three real mistakes in the GitHub contract:
    a spec describing fields nothing reads has drifted from the tool, and will
    keep passing while describing a program that no longer exists."""
    described = {
        field.primary
        for group in (SEARCH_RESULT_FIELDS, CITATION_FIELDS, USAGE_FIELDS, WEB_SEARCH.envelope)
        for field in group
    }
    # encrypted_content is described precisely because the parser drops it,
    # and saying so is the point of that entry.
    described.discard("encrypted_content")

    unread = described - FIELDS_THE_PARSER_READS
    assert unread == set(), f"the contract describes fields no code reads: {sorted(unread)}"


@pytest.mark.parametrize(
    "name", ["url", "title", "cited_text", "server_tool_use", "web_search_requests"]
)
def test_the_load_bearing_field_names_appear_in_the_parser(name: str) -> None:
    source = Path(__file__).parents[1] / "corpus" / "search" / "providers.py"
    assert name in source.read_text(), f"{name} is in the contract but not in providers.py"


def test_the_contract_names_the_block_types_the_parser_switches_on() -> None:
    source = (Path(__file__).parents[1] / "corpus" / "search" / "providers.py").read_text()
    for block in (BLOCK_TOOL_RESULT, BLOCK_RESULT):
        assert block in source


def test_a_paused_search_turn_is_reported_rather_than_read_as_no_results() -> None:
    """`pause_turn` means the server stopped mid-search. Whatever arrived is
    partial, and silence about it reads as "this person has no web presence"."""
    payload = response()
    payload["stop_reason"] = "pause_turn"

    results, errors = results_from_message(payload, limit=10)

    assert results, "the partial results that did arrive are still worth keeping"
    assert any("pause_turn" in e for e in errors)
