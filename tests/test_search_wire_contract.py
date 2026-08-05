"""The offline half of the search wire contract.

`corpus/search/contract.py` states what `results_from_message` depends on;
this checks the fixture against it on every test run, and
`scripts/verify_search_contract.py` checks the live API against the same spec.
One spec, two checkers.

What this can and cannot prove, stated plainly because the distinction is the
entire point: the fixture is now a **captured** response (2026-08-03, scrubbed
by `tests/fixtures/_scrub_search.py`), so agreement here is agreement with a
payload the API really sent. What it still cannot prove is that the API has not
moved since — that is what the live checker is for, and `WEB_SEARCH.verified`
records when it last ran.
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
CITED_FIXTURE = Path(__file__).parent / "fixtures" / "web_search_response_with_citations.json"

#: The `web_search_tool_result` block sits second, after the `server_tool_use`
#: that ran the query. That order is from the capture, not a convention.
TOOL_RESULT = 1


def response() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def cited_response() -> dict[str, Any]:
    """SYNTHETIC. No live response has ever carried a citation."""
    return json.loads(CITED_FIXTURE.read_text())


def severities(payload: Any) -> set[str]:
    return {v.severity for v in check_search_response(payload)}


# -- the fixture satisfies the contract -------------------------------------


def test_the_fixture_satisfies_the_contract() -> None:
    violations = check_search_response(response())
    assert violations == [], "\n".join(str(v) for v in violations)


def test_the_contract_records_when_it_was_checked_against_the_wire() -> None:
    """This used to assert the opposite — that `verified` was still empty — and
    it was the reminder to record what a live run saw. The live run happened;
    what has to stay true now is that the claim names a date and a way to
    repeat it, rather than being an unfalsifiable "verified"."""
    assert WEB_SEARCH.is_verified
    assert "2026-08-03" in WEB_SEARCH.verified
    assert "verify_search_contract" in WEB_SEARCH.verified


def test_the_fixture_says_it_came_from_the_wire() -> None:
    payload = response()
    assert payload["_provenance"].startswith("REAL.")
    assert "_scrub_search.py" in payload["_provenance"]


def test_the_citation_fixture_admits_it_is_not_a_capture() -> None:
    """The path production cannot reach: no live response has carried a
    citation, so the only thing keeping the snippet code honest is a fixture
    that says out loud it was written rather than observed."""
    assert cited_response()["_provenance"].startswith("SYNTHETIC")


def test_the_captured_response_carries_no_citations() -> None:
    """The finding, pinned. If a future capture *does* carry one, this test
    fails and the empty-snippet reasoning throughout Phase 2 gets revisited."""
    payload = response()
    text_blocks = [b for b in payload["content"] if b.get("type") == "text"]

    assert text_blocks and all(b.get("citations") is None for b in text_blocks)


# -- the shapes that would fail silently ------------------------------------


def test_a_renamed_result_block_is_caught() -> None:
    """The silent failure this file exists for. A zero-result search looks
    exactly like a target the anchors already cover — nothing in the report
    would look wrong."""
    payload = response()
    payload["content"][TOOL_RESULT]["type"] = "web_search_tool_result_v2"

    violations = check_search_response(payload)

    assert worst_severity(violations) == CRITICAL
    assert any(BLOCK_TOOL_RESULT in str(v) for v in violations)
    # And the parser really does go quiet, which is why it is critical.
    assert results_from_message(payload, limit=10)[0] == []


def test_a_result_without_a_url_is_critical() -> None:
    payload = response()
    del payload["content"][TOOL_RESULT]["content"][0]["url"]

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
    payload["content"][TOOL_RESULT]["content"] = "two results"

    assert CRITICAL in severities(payload)


# -- shapes that are fine, and must not cry wolf ----------------------------


def test_an_errored_search_is_not_a_contract_violation() -> None:
    """A rate limit is the API working. Reporting it as drift would train a
    reader to ignore the report."""
    payload = response()
    payload["content"][TOOL_RESULT]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "too_many_requests",
    }

    assert check_search_response(payload) == []


def test_an_undocumented_error_code_is_noted_but_not_fatal() -> None:
    payload = response()
    payload["content"][TOOL_RESULT]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "brand_new_code",
    }

    violations = check_search_response(payload)

    assert violations and worst_severity(violations) != CRITICAL


def test_a_result_with_no_page_age_is_fine() -> None:
    payload = response()
    for item in payload["content"][TOOL_RESULT]["content"]:
        item.pop("page_age", None)

    assert check_search_response(payload) == []


def test_a_response_with_no_citations_is_not_reported_as_drift() -> None:
    """It used to be, and it fired on all nine live responses.

    Every candidate does reach unconfirmed.md with no preview text — but that
    is what this tool's own search prompt produces, not something the API did
    to us. A checker that flags the normal case on every run teaches its reader
    to skip the line that matters, the same reason a rate-limited search is not
    a violation either.
    """
    assert check_search_response(response()) == []


def test_a_citation_that_arrives_with_a_renamed_field_is_still_caught() -> None:
    """Absence is the norm; a renamed `cited_text` on a citation that did
    arrive is drift, and the snippet would go silently empty."""
    payload = cited_response()
    for block in payload["content"]:
        for citation in block.get("citations") or []:
            citation["quoted_text"] = citation.pop("cited_text")

    violations = check_search_response(payload)

    assert any("cited_text" in str(v) for v in violations)


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


# -- the live checker's own guard -------------------------------------------
# It runs by hand, a few times a year, and nothing else would catch it
# breaking. Its job is not only to print numbers but to refuse to print the
# ones it knows are an artifact — which it failed to do on 2026-08-03, when it
# reported a full census of a run that read zero of 50 candidate pages.


def live_checker() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_search_contract.py"
    spec = importlib.util.spec_from_file_location("verify_search_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # import alone must make no network call
    return module


def phase_result(**kwargs: Any) -> Any:
    from corpus.search.verify import SearchCandidate, SearchPhaseResult

    result = SearchPhaseResult(**kwargs)
    return result, SearchCandidate


def test_the_live_checker_is_importable_and_offline_safe() -> None:
    module = live_checker()
    assert module.REACHABILITY_PROBE
    assert hasattr(module, "census")


def test_a_census_over_candidates_nobody_read_is_refused() -> None:
    """The bug this guard exists for. Zero pages read is exactly the condition
    that makes every held count meaningless, and it is indistinguishable in the
    output from a threshold set too high."""
    module = live_checker()
    result, candidate = phase_result(reads_attempted=26)
    result.held = [candidate(url=f"https://site{i}.example/p") for i in range(26)]

    reason = module.artifact_reason(result)

    assert reason, "a run that read nothing must not have its census printed"
    assert "26" in reason and "threshold" in reason


def test_a_census_backed_by_read_pages_is_printed() -> None:
    module = live_checker()
    result, candidate = phase_result(reads_attempted=3)
    result.held = [candidate(url="https://a.example/p", verified=True)]
    result.candidates = [candidate(url="https://b.example/p", verified=True)]

    assert module.artifact_reason(result) == ""


def test_a_common_name_refusal_is_not_reported_as_calibration() -> None:
    """Zero corroborated by refusal is not zero corroborated by scoring."""
    module = live_checker()
    result, candidate = phase_result(reads_attempted=9, common_name=True)
    result.held = [candidate(url=f"https://site{i}.example/p", verified=True) for i in range(9)]

    reason = module.artifact_reason(result)

    assert "conflicting identity facts" in reason


def test_a_run_that_found_nothing_is_not_called_an_artifact() -> None:
    """No results is a real answer about a target, and crying wolf about it
    would train a reader to skip the warning that matters."""
    module = live_checker()
    result, _ = phase_result()

    assert module.artifact_reason(result) == ""


def test_a_paused_search_turn_is_reported_rather_than_read_as_no_results() -> None:
    """`pause_turn` means the server stopped mid-search. Whatever arrived is
    partial, and silence about it reads as "this person has no web presence"."""
    payload = response()
    payload["stop_reason"] = "pause_turn"

    results, errors = results_from_message(payload, limit=10)

    assert results, "the partial results that did arrive are still worth keeping"
    assert any("pause_turn" in e for e in errors)
