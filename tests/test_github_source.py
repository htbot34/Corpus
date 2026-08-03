"""The GitHub adapter, against payloads captured from the live API.

Two of these tests exist because of what the capture actually contained, not
because of what the documentation says:

* `test_a_search_result_is_never_the_subjects_writing` — in the capture, 0 of
  20 items returned by `search/issues?q=commenter:X` were authored by the
  subject. An adapter that read `item.body` would attribute twenty strangers'
  pull-request descriptions to the target.
* `test_a_push_with_no_commits_is_counted_not_ignored` — 69 of 69 PushEvents
  carried no `commits` array at all. "GitHub stopped sending these" and "this
  person pushed nothing" must not produce the same report.

Everything runs from a pre-seeded cache; `http_client` is replaced with
something that fails the test if it is ever constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.cache import Cache
from corpus.sources import github as github_module
from corpus.sources.github import (
    API,
    MAX_THREADS_ANONYMOUS,
    GitHubSource,
    classify_commit,
    fetch_profile,
)

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN = "testsubject"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def cache(tmp_path: Path) -> Cache:
    c = Cache(path=tmp_path / "cache.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the github adapter opened an HTTP client during a test")

    monkeypatch.setattr(github_module, "http_client", forbidden)


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    """Default to the anonymous path; the token tests set it explicitly."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def seed_events(cache: Cache, events, pages: int = 1) -> None:
    for page in range(1, pages + 2):
        payload = events if page == 1 else []
        cache.put("github", f"{API}/users/{LOGIN}/events/public?per_page=100&page={page}", payload)


def seed_search(cache: Cache, search) -> None:
    cache.put(
        "github",
        f"{API}/search/issues?q=commenter:{LOGIN}+type:pr&per_page=30&sort=updated&order=desc",
        search,
    )


def read(cache: Cache, **kwargs):
    return GitHubSource().fetch_with_stats(
        LOGIN, author_handle=LOGIN, cache=cache, log=lambda _: None, **kwargs
    )


# -- the commit-message filter ---------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "wip",
        "WIP: do not merge",
        "fix typo",
        "Fixed typos",
        "typo",
        "Merge branch 'fixes' of https://github.com/someone/project",
        "Merge pull request #12 from someone/branch",
        "formatting",
        "bump version to 1.2.3",
        "update readme",
        "tweak",
        "",
    ],
)
def test_commit_messages_with_no_argument_in_them_are_dropped(message: str) -> None:
    assert classify_commit(message) is not None


@pytest.mark.parametrize(
    "message",
    [
        "audio: keep the gate's threshold in fixed point\n\nFloating point in the "
        "interrupt path cost more than the precision was worth.",
        "gen_effects: sort by (priority, filename) so ids are stable",
        'Revert "audio: fixed point"\n\nThe Q15 range is wrong for the high-gain path.',
        # A short subject with a body is someone explaining themselves.
        "fix races\n\nThe lock was taken after the check, which is the wrong order.",
    ],
)
def test_commit_messages_that_carry_reasoning_are_kept(message: str) -> None:
    assert classify_commit(message) is None


def test_the_filter_reads_the_subject_line_not_the_whole_message() -> None:
    """A substantive commit that happens to mention a typo is not a typo fix."""
    assert (
        classify_commit(
            "parser: accept a trailing comma\n\nThis started as a typo fix and "
            "turned into a grammar question: the spec allows it."
        )
        is None
    )


# -- events ------------------------------------------------------------------


def test_review_comments_carry_the_diff_hunk_as_context(cache: Cache) -> None:
    """Same rule as a reply's parent tweet: "keep a bitmap instead" is
    unreadable without the code it is about."""
    seed_events(cache, fixture("github_events.json"))
    docs, stats = read(cache, search_threads=False)

    review = [d for d in docs if d.raw.get("kind") == "review_comment"]
    assert len(review) == stats.review_comments == 1
    assert review[0].context, "a review comment with no diff hunk is not legible"
    assert "@@" in review[0].context
    assert review[0].kind == "reply"
    assert review[0].url.startswith("https://github.com/")


def test_issue_comments_take_the_thread_title_as_context_not_its_body(cache: Cache) -> None:
    """The issue body belongs to whoever opened the thread. The title is enough
    to make the comment legible and cannot be mistaken for a paragraph the
    subject wrote."""
    seed_events(cache, fixture("github_events.json"))
    docs, stats = read(cache, search_threads=False)

    comments = [d for d in docs if d.raw.get("kind") == "issue_comment"]
    assert len(comments) == stats.issue_comments == 5
    bodies = " ".join(d.body for d in comments)
    assert "Synthetic issue body" not in bodies, "an issue body was ingested as the subject's"
    for doc in comments:
        assert doc.context and doc.context.startswith("testsubject/")


def test_a_comment_by_someone_else_on_the_subjects_feed_is_skipped(cache: Cache) -> None:
    """`actor` and `comment.user` agreed on all 16 comment events in the
    capture. Both are checked anyway; the cost is one comparison."""
    events = fixture("github_events.json")
    for event in events:
        if event["type"] == "IssueCommentEvent":
            event["payload"]["comment"]["user"]["login"] = "someone-else"
    seed_events(cache, events)
    docs, stats = read(cache, search_threads=False)
    assert stats.issue_comments == 0
    assert not [d for d in docs if d.raw.get("kind") == "issue_comment"]


def test_a_push_with_no_commits_is_counted_not_ignored(cache: Cache) -> None:
    """The observed case: 69 of 69 pushes carried no `commits` array. A report
    that says nothing here is indistinguishable from an account that pushed
    nothing."""
    seed_events(cache, fixture("github_events.json"))
    docs, stats = read(cache, search_threads=False)

    assert stats.push_events == 4
    assert stats.push_events_without_commits == 4
    assert stats.commits_kept == 0
    assert not [d for d in docs if d.raw.get("kind") == "commit"]
    assert any("carried no commit messages" in note for note in stats.notes)
    assert any("not an absence of commits" in note for note in stats.notes)


def test_commits_are_extracted_and_filtered_when_github_sends_them(cache: Cache) -> None:
    """Against the documented PushEvent shape — see the fixture's name."""
    seed_events(cache, fixture("github_events_with_commits.json"))
    docs, stats = read(cache, search_threads=False)

    commits = [d for d in docs if d.raw.get("kind") == "commit"]
    assert stats.commits_kept == len(commits) == 3
    assert stats.commits_filtered == 4
    assert set(stats.filtered_by_reason) == {"no_content", "merge_commit"}
    assert stats.push_events_without_commits == 0

    kept = " ".join(d.body for d in commits)
    assert "fixed point" in kept
    assert "wip" not in kept.lower()
    assert "Merge branch" not in kept
    assert commits[0].url.startswith("https://github.com/testsubject/")


def test_pull_request_events_contribute_nothing(cache: Cache) -> None:
    """Observed: payload.pull_request is trimmed to {base, head, id, number,
    url}. There is no title and no body to read, so the adapter must not
    pretend otherwise."""
    events = [e for e in fixture("github_events.json") if e["type"] == "PullRequestEvent"]
    assert events, "the fixture lost its PullRequestEvents"
    seed_events(cache, events)
    docs, _ = read(cache, search_threads=False)
    assert docs == []


def test_the_events_window_is_stated_in_the_notes(cache: Cache) -> None:
    seed_events(cache, fixture("github_events.json"))
    _, stats = read(cache, search_threads=False)
    assert any("90 days" in n and "300 events" in n for n in stats.notes)


# -- search, and the misattribution it would otherwise cause -----------------


def test_a_search_result_is_never_the_subjects_writing(cache: Cache) -> None:
    """The finding that shaped the adapter. Every item in the capture was a PR
    opened by someone else; `item.body` is their description."""
    search = fixture("github_search_pr.json")
    assert all(i["user"]["login"] != LOGIN for i in search["items"])

    seed_events(cache, [])
    seed_search(cache, search)
    # No thread bodies are seeded, so every comments_url read misses.
    docs, stats = read(cache)

    assert docs == [], "a pull request description was ingested as the subject's writing"
    assert stats.threads_searched == 5
    assert stats.search_total == 179
    bodies = " ".join(i["body"] for i in search["items"])
    assert "biquad" in bodies, "the fixture no longer contains other people's prose"


def test_only_the_subjects_comments_are_kept_from_a_thread(cache: Cache) -> None:
    search = fixture("github_search_pr.json")
    seed_events(cache, [])
    seed_search(cache, search)

    thread = [
        {
            "id": 1,
            "user": {"login": "contributor-one"},
            "body": "I think the priority tie is harmless.",
            "html_url": "https://github.com/testsubject/project-alpha/pull/26#issuecomment-1",
            "created_at": "2026-03-01T10:00:00Z",
        },
        {
            "id": 2,
            "user": {"login": LOGIN},
            "body": "It is not harmless. A tie means the id depends on the filesystem.",
            "html_url": "https://github.com/testsubject/project-alpha/pull/26#issuecomment-2",
            "created_at": "2026-03-01T11:00:00Z",
        },
    ]
    cache.put("github", f"{search['items'][0]['comments_url']}?per_page=100", thread)

    docs, stats = read(cache)
    assert stats.thread_comments_kept == 1
    assert stats.thread_comments_by_others == 1
    assert len(docs) == 1
    assert docs[0].body.startswith("It is not harmless")
    assert docs[0].raw["kind"] == "thread_comment"
    assert "harmless the priority tie" not in docs[0].body


def test_the_search_reaches_further_back_than_the_events_feed(cache: Cache) -> None:
    """Why the search path is worth its rate-limit cost at all."""
    search = fixture("github_search_pr.json")
    events = fixture("github_events.json")
    newest_search = max(i["created_at"] for i in search["items"])
    oldest_search = min(i["created_at"] for i in search["items"])
    oldest_event = min(e["created_at"] for e in events)
    assert oldest_search < oldest_event <= newest_search or oldest_search < oldest_event


def test_the_search_rate_limit_is_reported(cache: Cache) -> None:
    seed_events(cache, [])
    seed_search(cache, fixture("github_search_pr.json"))
    _, stats = read(cache)
    assert any("30 requests per minute" in n for n in stats.notes)


def test_exactly_one_search_request_is_issued(cache: Cache) -> None:
    """Search is 30/minute, separately from core. Paginating it would trade a
    strict limit for marginal coverage."""
    seen: list[str] = []
    real_get = github_module._Reader.get

    def spy(self, url: str):
        seen.append(url)
        return real_get(self, url)

    seed_events(cache, [])
    seed_search(cache, fixture("github_search_pr.json"))
    original = github_module._Reader.get
    github_module._Reader.get = spy  # type: ignore[method-assign]
    try:
        read(cache)
    finally:
        github_module._Reader.get = original  # type: ignore[method-assign]
    assert len([u for u in seen if "/search/issues" in u]) == 1


def test_anonymous_runs_open_fewer_threads(cache: Cache, monkeypatch) -> None:
    """60 requests an hour is the whole budget; 25 thread reads would be a
    third of it spent on one target."""
    search = fixture("github_search_pr.json")
    search["items"] = search["items"] * 8  # 40 items
    seed_events(cache, [])
    seed_search(cache, search)
    for item in search["items"]:
        cache.put("github", f"{item['comments_url']}?per_page=100", [])

    _, stats = read(cache)
    assert stats.threads_read <= MAX_THREADS_ANONYMOUS
    assert any("Unauthenticated" in n for n in stats.notes)


def test_a_token_raises_the_thread_cap_and_the_note_goes_away(cache: Cache, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_notreal")
    search = fixture("github_search_pr.json")
    search["items"] = search["items"] * 8
    seed_events(cache, [])
    seed_search(cache, search)
    for item in search["items"]:
        cache.put("github", f"{item['comments_url']}?per_page=100", [])

    _, stats = read(cache)
    assert stats.authenticated
    assert stats.threads_read > MAX_THREADS_ANONYMOUS
    assert not any("Unauthenticated" in n for n in stats.notes)


def test_the_token_is_sent_as_a_bearer_header_when_present(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_notreal")
    assert github_module._headers()["Authorization"] == "Bearer ghp_notreal"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert "Authorization" not in github_module._headers()


# -- degradation -------------------------------------------------------------


def test_an_unreachable_api_degrades_the_corpus_not_the_run(cache: Cache) -> None:
    """Nothing is seeded and the client is forbidden, so every request fails."""
    docs, stats = read(cache)
    assert docs == []
    assert stats.notes, "a total failure produced no explanation"


def test_documents_are_deduplicated_across_the_two_paths(cache: Cache) -> None:
    """A comment can arrive from the events feed and from a searched thread."""
    events = fixture("github_events.json")
    comment = next(e for e in events if e["type"] == "IssueCommentEvent")["payload"]["comment"]
    search = fixture("github_search_pr.json")
    seed_events(cache, events)
    seed_search(cache, search)
    cache.put("github", f"{search['items'][0]['comments_url']}?per_page=100", [comment])

    docs, _ = read(cache)
    ids = [d.source_id for d in docs]
    assert len(ids) == len(set(ids))


def test_documents_are_ordered_newest_first_and_capped(cache: Cache) -> None:
    seed_events(cache, fixture("github_events.json"))
    docs, _ = read(cache, search_threads=False, limit=3)
    assert len(docs) == 3
    assert [d.published_at for d in docs] == sorted((d.published_at for d in docs), reverse=True)


def test_the_profile_read_returns_the_identity_signals(cache: Cache) -> None:
    cache.put("github", f"{API}/users/{LOGIN}", fixture("github_user.json"))
    profile = fetch_profile(LOGIN, cache, log=lambda _: None)
    assert profile["login"] == LOGIN
    assert profile["company"] == "Acme Corp"
    assert profile["location"] == "Portland, OR"


def test_every_document_is_a_github_document_attributed_to_the_subject(cache: Cache) -> None:
    seed_events(cache, fixture("github_events.json"))
    docs, _ = read(cache, search_threads=False)
    assert docs
    for doc in docs:
        assert doc.source == "github"
        assert doc.author_handle == LOGIN
        # The adapter never sets attribution; the caller stamps it, because an
        # adapter cannot know how its target was arrived at.
        assert doc.attribution == "anchor"
