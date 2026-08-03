"""The GitHub fixtures, checked against the contract, on every test run.

Same job as `test_wire_contract.py` does for twitterapi.io. The difference is
the evidence behind it: these fixtures came from the live API on 2026-08-03, so
a failure here means GitHub changed something, not that a guess was wrong.

The point is to make a renamed field loud. Every reader in `github.py` has a
fallback — `.get(...) or ""` — which is right for robustness and is exactly
what turns a breaking change into a silent one: a renamed `body` yields zero
documents and exit code 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.sources import github_contract as gc
from corpus.sources.github import GitHubSource
from corpus.x.contract import CRITICAL, IMPORTANT, worst_severity

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("endpoint", "name"),
    [
        ("github_user", "github_user.json"),
        ("github_events_public", "github_events.json"),
        ("github_search_issues_commenter", "github_search_pr.json"),
    ],
)
def test_the_captured_fixtures_satisfy_the_contract(endpoint: str, name: str) -> None:
    violations = gc.check(endpoint, fixture(name))
    assert violations == [], "\n".join(str(v) for v in violations)


def test_a_renamed_critical_field_is_caught(endpoint_free: None = None) -> None:
    """A scanner that always passes is worse than none. Rename the field the
    adapter cannot work without and the contract must say so."""
    events = fixture("github_events.json")
    for event in events:
        event["payloadV2"] = event.pop("payload")
    violations = gc.check("github_events_public", events)
    assert worst_severity(violations) == CRITICAL
    assert any("payload" in str(v) for v in violations)


def test_an_envelope_that_becomes_an_array_is_caught() -> None:
    """search/issues returns an object. If it ever returns a bare array the
    adapter reads nothing, so the shape itself is part of the contract."""
    violations = gc.check(
        "github_search_issues_commenter", fixture("github_search_pr.json")["items"]
    )
    assert worst_severity(violations) == CRITICAL
    assert any("expected a JSON object" in str(v) for v in violations)


def test_an_array_that_becomes_an_envelope_is_caught() -> None:
    """events/public returns a bare array — no envelope. The X endpoints all
    wrap, so this is the one shape difference worth pinning."""
    violations = gc.check("github_events_public", {"events": fixture("github_events.json")})
    assert worst_severity(violations) == CRITICAL
    assert any("no item array found" in str(v) for v in violations)


def test_a_missing_comments_url_is_critical() -> None:
    """It is the only field on a search item that leads to the subject's own
    words. Without it the search path silently yields nothing."""
    search = fixture("github_search_pr.json")
    for item in search["items"]:
        item.pop("comments_url")
    violations = gc.check("github_search_issues_commenter", search)
    assert worst_severity(violations) == CRITICAL


def test_a_null_field_is_not_a_missing_field() -> None:
    """GitHub returns null constantly for fields that exist and are unset. An
    absent key means a rename; a null means the account has no bio."""
    user = fixture("github_user.json")
    user["bio"] = None
    assert gc.check("github_user", user) == []

    del user["bio"]
    violations = gc.check("github_user", user)
    assert any("bio" in str(v) for v in violations)


# -- the two findings, asserted so they cannot be quietly forgotten ---------


def test_no_captured_push_event_carries_commits() -> None:
    """The finding that shaped the adapter, pinned against the fixture.

    If this ever fails because the fixture regained a `commits` array, GitHub
    started sending them again and the coverage note in `_add_coverage_notes`
    is now wrong.
    """
    pushes = [e for e in fixture("github_events.json") if e["type"] == "PushEvent"]
    assert pushes
    assert all("commits" not in e["payload"] for e in pushes)
    assert all("size" not in e["payload"] for e in pushes)
    # Undocumented, and present on every one.
    assert all("repository_id" in e["payload"] for e in pushes)


def test_no_captured_search_item_was_written_by_the_subject() -> None:
    """The other finding. `item.body` belongs to whoever opened the PR, so the
    adapter must never treat a search item as a document."""
    items = fixture("github_search_pr.json")["items"]
    assert items
    assert all(i["user"]["login"] != "testsubject" for i in items)
    assert all(i.get("body") is not None for i in items[:1])


def test_the_synthetic_commit_fixture_is_labelled_as_such() -> None:
    """It is the one GitHub fixture with no capture behind it, and its filename
    is the label. A future reader must not mistake it for evidence."""
    path = FIXTURES / "github_events_with_commits.json"
    assert path.exists()
    assert "with_commits" in path.name
    events = fixture(path.name)
    assert all("commits" in e["payload"] for e in events)


def test_unverified_endpoints_are_declared_unverified() -> None:
    """Confirming an endpoint without updating the contract is a red test.

    `github_issue_comments` is reached only from a search result's
    `comments_url`, and no capture of that response exists — the element shape
    is inferred from the identical comment object embedded in
    IssueCommentEvent, which *was* captured.
    """
    assert gc.unverified_endpoints() == ["github_issue_comments"]


def test_the_contract_states_both_measured_limits() -> None:
    """A reader of the coverage block should be able to find the numbers."""
    assert gc.EVENTS_WINDOW_DAYS == 90
    assert gc.EVENTS_MAX_EVENTS == 300
    assert gc.SEARCH_LIMIT_PER_MINUTE == 30
    assert gc.UNAUTHENTICATED_CORE_PER_HOUR == 60
    notes = " ".join(gc.SEARCH_ISSUES_COMMENTER.notes)
    assert "per MINUTE" in notes
    assert "0 of 20" in notes


def test_the_contract_agrees_with_what_the_adapter_reads() -> None:
    """The failure this whole file exists to prevent is the contract and the
    code drifting apart. Every field named critical must be one the adapter
    would actually break without."""
    source = (Path(__file__).parents[1] / "corpus" / "sources" / "github.py").read_text()
    for contract in (gc.EVENTS_PUBLIC, gc.SEARCH_ISSUES_COMMENTER, gc.ISSUE_COMMENTS):
        for spec in contract.items:
            if spec.severity not in (CRITICAL, IMPORTANT):
                continue
            assert f'"{spec.primary}"' in source or f"'{spec.primary}'" in source, (
                f"{contract.name}.{spec.primary} is {spec.severity} in the contract "
                "but is never read in github.py"
            )


def test_the_adapter_reads_nothing_the_contract_does_not_declare() -> None:
    """The other direction: a field the code depends on and the contract does
    not mention is a field nobody is watching."""
    declared = {
        spec.primary
        for contract in gc.CONTRACTS.values()
        for spec in (*contract.items, *contract.envelope)
    }
    declared.update({spec.primary for spec in gc.PUSH_COMMIT_FIELDS})
    # Read out of nested objects rather than off the item itself, so they are
    # covered by the parent field's contract entry.
    nested = {
        "comment",
        "issue",
        "diff_hunk",
        "path",
        "pull_request_url",
        "commits",
        "sha",
        "message",
        "name",
        "login",
        "total_count",
        "items",
    }
    source = (Path(__file__).parents[1] / "corpus" / "sources" / "github.py").read_text()
    import re

    # Single-argument .get() only: `cache.get("github", url)` is a cache
    # lookup keyed by source name, not a field the wire has to provide.
    read_keys = set(re.findall(r'\.get\("([a-z_]+)"(?!\s*,)', source))
    undeclared = read_keys - declared - nested
    assert not undeclared, f"github.py reads undeclared fields: {sorted(undeclared)}"


def test_the_source_protocol_is_satisfied() -> None:
    from corpus.sources.base import SecondarySource

    assert isinstance(GitHubSource(), SecondarySource)
