"""Reading a search response, and capturing it verbatim.

The fixture is a **real response**, captured on 2026-08-03 and scrubbed — see
`tests/fixtures/_scrub_search.py`. So these tests prove the parser reads what
the API actually sends, which is what `--capture-search` existed to make
possible and is why it is still tested here.

One path has no live example and never will while the search system prompt
stands: the model is told to answer with the single word "done", a model that
writes nothing cites nothing, and citations are the only place a snippet can
come from. `web_search_response_with_citations.json` is written to the
documented shape and labelled SYNTHETIC for exactly that reason.

Two shapes matter more than the happy path:

* an errored search is a **200** with an error object where the result list
  belongs, so a caller that only catches exceptions sees silence;
* a search that matched nothing returns an **empty list**, which is a finding
  and not a failure. Confusing the two would turn "no web presence" and "the
  rate limiter said no" into the same report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpus.search.capture import SearchCapture
from corpus.search.providers import (
    PROVIDERS,
    SEARCH_TOOL_TYPE,
    AnthropicSearchProvider,
    SearchError,
    get_search_provider,
    parse_page_age,
    results_from_message,
    usage_from_message,
)

FIXTURES = Path(__file__).parent / "fixtures"


def response() -> dict[str, Any]:
    """The real capture: no citations, so no snippets."""
    return json.loads((FIXTURES / "web_search_response.json").read_text())


def cited_response() -> dict[str, Any]:
    """SYNTHETIC — the documented citation shape. See the module docstring."""
    return json.loads((FIXTURES / "web_search_response_with_citations.json").read_text())


#: Index of the `web_search_tool_result` block. Second, after the
#: `server_tool_use` that ran the query — the order every capture arrived in.
TOOL_RESULT = 1


class StubMessages:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.payload


class StubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.messages = StubMessages(payload)


# -- reading the response ---------------------------------------------------


def test_results_come_out_of_the_tool_result_blocks() -> None:
    results, errors = results_from_message(response(), limit=10)

    assert errors == []
    assert len(results) == 10, "the capture returned ten; RESULTS_PER_QUERY is what trims them"
    assert results[0].url == "http://janesmith.example/blog/tutorial-on-inference-networks-2"
    assert results[0].title == "Inference networks | Jane Smith"


def test_a_real_response_carries_no_snippet_at_all() -> None:
    """Not a defect in the fixture — the finding the live run produced.

    A `web_search_result` has no snippet field, snippets are built from the
    model's citations, and the search system prompt tells the model to write
    one word. Nine of nine live responses came back `citations: null`, so every
    candidate reaches scoring with an empty snippet. Nothing may depend on one.
    """
    payload = response()
    text_block = next(b for b in payload["content"] if b["type"] == "text")
    assert text_block["citations"] is None

    results, _ = results_from_message(payload, limit=10)

    assert all(r.snippet == "" for r in results)


def test_the_snippet_comes_from_the_model_citation_for_that_url() -> None:
    """SYNTHETIC fixture: `cited_text` is a verbatim quotation, matched to its
    result by URL, and it is not billed as tokens. Unreachable in production
    today — see the module docstring — which is why it is pinned here."""
    results, _ = results_from_message(cited_response(), limit=10)

    assert results[0].snippet.startswith("Rubrics beat interviews because")
    assert results[1].snippet == "", "a result the model did not cite has no snippet"


def test_a_page_age_becomes_a_date_and_an_unreadable_one_becomes_nothing() -> None:
    results, _ = results_from_message(response(), limit=10)

    assert results[0].published_at is not None
    assert results[0].published_at.year == 2016
    assert results[1].published_at is None, "a date we cannot read must not be guessed at"


def test_the_encrypted_blob_is_not_kept() -> None:
    """Kilobytes per result, meaningful only when replayed to the same API,
    and discovery.json has to stay readable."""
    results, _ = results_from_message(response(), limit=10)

    assert "encrypted_content" not in results[0].raw
    assert set(results[0].raw) <= {"url", "title", "page_age"}


def test_the_limit_is_honoured() -> None:
    results, _ = results_from_message(response(), limit=1)
    assert len(results) == 1


def test_an_errored_search_is_reported_rather_than_read_as_empty() -> None:
    payload = response()
    payload["content"][TOOL_RESULT]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "max_uses_exceeded",
    }

    results, errors = results_from_message(payload, limit=10)

    assert results == []
    assert errors == ["max_uses_exceeded"]


def test_a_search_that_matched_nothing_is_not_an_error() -> None:
    payload = response()
    payload["content"][TOOL_RESULT]["content"] = []

    results, errors = results_from_message(payload, limit=10)

    assert results == []
    assert errors == [], "an empty result list is a finding, not a failure"


def test_a_duplicate_url_across_blocks_is_returned_once() -> None:
    payload = response()
    payload["content"].append(payload["content"][TOOL_RESULT])

    results, _ = results_from_message(payload, limit=20)

    assert len(results) == 10


def test_usage_reports_the_billing_unit() -> None:
    """`web_search_requests` is what the invoice counts, not the API call."""
    usage = usage_from_message(response(), "claude-haiku-4-5-20251001")

    assert usage.searches == 1
    assert usage.input_tokens == 9921
    assert usage.output_tokens == 70
    assert usage.model == "claude-haiku-4-5-20251001"


def test_a_response_with_no_usage_block_does_not_explode() -> None:
    usage = usage_from_message({"content": []}, "m")
    assert usage.searches == 0


@pytest.mark.parametrize(
    ("value", "year"),
    [("April 30, 2025", 2025), ("2024-03-12", 2024), ("Apr 3, 2020", 2020), ("2019", 2019)],
)
def test_page_age_formats_seen_in_the_wild(value: str, year: int) -> None:
    parsed = parse_page_age(value)
    assert parsed is not None and parsed.year == year


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "sometime last year",
        "not a date",
        # Observed on the wire: page_age is not always an absolute date.
        "1 month ago",
    ],
)
def test_an_unparseable_page_age_is_none(value: object) -> None:
    assert parse_page_age(value) is None


# -- the request ------------------------------------------------------------


def test_the_request_pins_one_billable_search_per_query() -> None:
    """`max_uses=1` is what makes the pre-flight reservation exact: without
    it the model decides how many searches a query is worth, after we have
    already committed to paying for them."""
    client = StubClient(response())
    provider = AnthropicSearchProvider(api_key="k", client=client)

    provider.search('"Jane Smith" "Acme Corp"', 5)

    call = client.messages.calls[0]
    assert call["tools"] == [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 1}]
    assert call["messages"] == [{"role": "user", "content": '"Jane Smith" "Acme Corp"'}]
    assert provider.last_usage.searches == 1


def test_a_failing_call_becomes_a_search_error_not_a_crash() -> None:
    class Boom:
        class messages:
            @staticmethod
            def create(**_: Any) -> Any:
                raise RuntimeError("connection reset")

    provider = AnthropicSearchProvider(api_key="k", client=Boom())

    with pytest.raises(SearchError) as caught:
        provider.search("q", 5)
    assert "connection reset" in str(caught.value)


def test_a_missing_key_says_what_to_do_about_it() -> None:
    with pytest.raises(SearchError) as caught:
        AnthropicSearchProvider(api_key="")
    assert "ANTHROPIC_API_KEY" in str(caught.value)
    assert "--no-search" in str(caught.value)


# -- the vendor seam --------------------------------------------------------


@pytest.mark.parametrize(("name", "env"), [("brave", "BRAVE_API_KEY")])
def test_an_unimplemented_provider_names_exactly_what_to_add(name: str, env: str) -> None:
    """The same courtesy corpus/x/providers.py extends: a stub that tells you
    the env var and the endpoint beats one that says NotImplementedError."""
    with pytest.raises(NotImplementedError) as caught:
        PROVIDERS[name]()

    message = str(caught.value)
    assert env in message
    assert "SearchResult" in message
    assert f"SEARCH_PROVIDER={name}" in message


def test_an_unknown_provider_lists_the_known_ones() -> None:
    with pytest.raises(SearchError) as caught:
        get_search_provider("nope")
    assert "anthropic_search" in str(caught.value)


def test_the_provider_is_selected_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_PROVIDER=exa reaches the real Exa provider, which refuses to
    construct without its key — proof both that selection worked and that the
    refusal happens at construction, before anything could be spent."""
    monkeypatch.setenv("SEARCH_PROVIDER", "exa")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(SearchError) as caught:
        get_search_provider()
    assert "EXA_API_KEY" in str(caught.value)


def test_no_client_is_built_until_a_search_actually_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a provider must not require a network route or a live key,
    or every test that merely mentions search acquires one."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    provider = get_search_provider()
    assert provider.name == "anthropic_search"
    provider.close()


# -- --capture-search -------------------------------------------------------


def test_capture_writes_the_raw_message_and_what_was_parsed(tmp_path: Path) -> None:
    client = StubClient(response())
    capture = SearchCapture(tmp_path)
    provider = AnthropicSearchProvider(api_key="k", client=client, capture=capture)

    provider.search('"Jane Smith" "Acme Corp"', 5)

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())

    assert payload["query"] == '"Jane Smith" "Acme Corp"'
    assert payload["usage"]["searches"] == 1
    # Both halves: what arrived, and what we made of it. When those two
    # disagree the capture is the only way to tell which one is wrong.
    assert payload["raw_message"]["content"][TOOL_RESULT]["type"] == "web_search_tool_result"
    assert (
        payload["parsed_results"][0]["url"]
        == "http://janesmith.example/blog/tutorial-on-inference-networks-2"
    )


def test_capture_never_writes_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deliberately not key-shaped: the secrets scanner refuses a real prefix in
    # a tracked file, and redaction works by exact value, not by shape.
    secret = "corpus-test-not-a-real-credential-value"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    payload = response()
    payload["content"][0]["input"]["query"] = f"echoed back: {secret}"

    SearchCapture(tmp_path).record(query="q", provider="p", model="m", message=payload, results=[])

    written = next(tmp_path.glob("*.json")).read_text()
    assert secret not in written
    assert "REDACTED" in written


def test_a_capture_that_cannot_be_written_costs_a_warning_not_the_search(
    tmp_path: Path,
) -> None:
    """The search was already paid for. A full disk must not also lose it."""
    logged: list[str] = []
    capture = SearchCapture(tmp_path, log=logged.append)
    capture.directory = tmp_path / "does" / "not" / "exist"

    assert capture.record(query="q", provider="p", model="m", message={}, results=[]) is None
    assert logged and "could not write" in logged[0]
