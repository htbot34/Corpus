"""Discovery through the actual CLI: `corpus profile`, and a run with no X.

The load-bearing test here is `test_a_run_with_no_x_anchor_works_end_to_end`.
X used to be mandatory and primary; a live provider returning zero posts for a
public account with 706 statuses is what ended that. If the tool cannot produce
a report from a blog alone, the discovery layer has not actually changed
anything.

Everything is offline: the provider is a fixture-backed fake, the model client
is a stub, and every URL discovery could reach is pre-seeded in the cache.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest
from fake_anthropic import FakeAnthropic
from fake_provider import FakeProvider
from fake_search import SilentSearchProvider
from typer.testing import CliRunner

from corpus import cli as cli_module
from corpus import discovery as discovery_module
from corpus.cache import Cache
from corpus.cli import app

runner = CliRunner()

FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>Jane</title>
{items}
</channel></rss>"""

ITEM = """<item>
<title>Essay {n}</title>
<link>https://janesmith.com/e{n}</link>
<pubDate>Tue, {day:02d} Mar 2024 10:00:00 +0000</pubDate>
<description>Rubrics beat interviews, and here is the argument in full. {n}</description>
<guid>https://janesmith.com/e{n}</guid>
</item>"""


def feed(count: int = 6) -> str:
    return FEED_XML.format(
        items="\n".join(ITEM.format(n=i, day=(i % 28) + 1) for i in range(count))
    )


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    """A CLI wired to fakes, with its own cache and profiles file."""
    monkeypatch.setenv("CORPUS_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("CORPUS_PROFILES", str(tmp_path / "profiles.yaml"))
    monkeypatch.setenv("X_API_KEY", "not-used-by-the-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-by-the-stub")
    monkeypatch.setattr(cli_module, "get_provider", lambda **_: FakeProvider())
    monkeypatch.setattr(
        cli_module, "synthesize", partial(cli_module.synthesize, client=FakeAnthropic())
    )
    monkeypatch.setattr(cli_module, "_ACTIVE_LOGGER", None)
    # Search runs by default; these tests are not about it, so it answers
    # with nothing and bills nothing rather than reaching a vendor.
    monkeypatch.setattr(cli_module, "get_search_provider", lambda **_: SilentSearchProvider())

    def forbidden(*args, **kwargs):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(discovery_module, "http_client", forbidden)
    return {
        "out": tmp_path / "out",
        "profiles": tmp_path / "profiles.yaml",
        "cache": tmp_path / "cache.db",
    }


def seed(wired, entries: dict[str, str], source: str = "discovery") -> None:
    cache = Cache(path=wired["cache"])
    for key, body in entries.items():
        cache.put(source, key, body)
    cache.close()


def invoke(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def outputs(base: Path, key: str) -> Path:
    days = sorted((base / key).iterdir())
    assert days, f"no output directory under {base / key}"
    return days[-1]


# ==========================================================================
# corpus profile
# ==========================================================================


def test_profile_writes_a_card_and_reads_it_back(wired) -> None:
    created = invoke(
        [
            "profile",
            "--name",
            "Jane Smith",
            "--employer",
            "Acme Corp",
            "--role",
            "VP Engineering",
            "--github",
            "jsmith",
            "--site",
            "https://janesmith.com",
            "--exclude",
            "https://linkedin.com/in/jane-smith-attorney",
        ]
    )
    assert created.exit_code == 0, created.output
    assert wired["profiles"].exists()

    shown = invoke(["profile", "--target", "janesmith"])
    assert "Jane Smith" in shown.output
    assert "github:jsmith" in shown.output
    assert "jane-smith-attorney" in shown.output


def test_profile_with_no_arguments_lists_what_is_on_file(wired) -> None:
    assert "no targets" in invoke(["profile"]).output
    invoke(["profile", "--name", "Jane Smith", "--github", "jsmith"])
    listed = invoke(["profile"])
    assert "janesmith" in listed.output
    assert "github:jsmith" in listed.output


def test_profile_updating_a_target_keeps_the_anchors_it_had(wired) -> None:
    invoke(["profile", "--name", "Jane Smith", "--github", "jsmith"])
    invoke(["profile", "--target", "janesmith", "--x", "janesmith"])
    shown = invoke(["profile", "--target", "janesmith"]).output
    assert "github:jsmith" in shown
    assert "x:janesmith" in shown


def test_profile_rejects_an_anchor_the_tool_cannot_follow(wired) -> None:
    result = invoke(["profile", "--name", "Jane Smith", "--github", "not a username"])
    assert result.exit_code == 2
    assert "GitHub username" in result.output


def test_a_card_with_no_anchors_is_saved_but_warned_about(wired) -> None:
    """Without an anchor there is nothing to score a name match against, which
    is the difference between discovery and guessing."""
    result = invoke(["profile", "--name", "Jane Smith"])
    assert result.exit_code == 0
    assert "no anchors" in result.output


# ==========================================================================
# a run with no X anchor at all
# ==========================================================================


def test_a_run_with_no_x_anchor_works_end_to_end(wired) -> None:
    """The whole point of the layer. A public account with 706 statuses
    returned zero posts from every X endpoint; the blog was readable the whole
    time."""
    seed(
        wired,
        {
            "get:https://janesmith.com": (
                '<html><head><link rel="alternate" type="application/rss+xml" '
                'href="/feed.xml"></head><body>hi</body></html>'
            )
        },
    )
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(8)}, source="rss")

    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--site",
            "https://janesmith.com",
            "--out",
            str(wired["out"]),
            "--yes",
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"], "janesmith")
    corpus = json.loads((directory / "corpus.json").read_text())
    assert len(corpus) == 8
    assert {d["source"] for d in corpus} == {"rss"}
    assert (directory / "report.md").exists()
    assert (directory / "synthesis.json").exists()

    report = (directory / "report.md").read_text()
    assert report.startswith("# Jane Smith — how they think"), (
        "a report with no X account should not be titled with an @handle"
    )


def test_a_zero_x_run_never_constructs_a_provider(wired, monkeypatch) -> None:
    """No X anchor means no X spend, and no key required."""

    def forbidden(**_):
        raise AssertionError("a run with no X anchor built an X provider")

    monkeypatch.setattr(cli_module, "get_provider", forbidden)
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(4)}, source="rss")

    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--rss",
            "https://janesmith.com/feed.xml",
            "--no-discover",
            "--out",
            str(wired["out"]),
            "--yes",
        ]
    )
    assert result.exit_code == 0, result.output
    assert (
        "X data (twitterapi.io): $0.0000"
        in (outputs(wired["out"], "janesmith") / "report.md").read_text()
    )


def test_a_run_with_nothing_to_read_says_so_before_spending(wired) -> None:
    result = invoke(["run", "--name", "Jane Smith", "--out", str(wired["out"]), "--yes"])
    assert result.exit_code == 2
    assert "no sources" in result.output


# ==========================================================================
# discovery inside a run
# ==========================================================================


def test_discovered_documents_carry_why_they_were_believed(wired) -> None:
    seed(
        wired,
        {
            "get:https://api.github.com/users/jsmith": json.dumps(
                {"blog": "https://janesmith.com", "bio": "VP Eng at Acme"}
            ),
            "get:https://janesmith.com": (
                '<html><head><link rel="alternate" type="application/rss+xml" '
                'href="/feed.xml"></head><body>hi</body></html>'
            ),
        },
    )
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(5)}, source="rss")

    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--github",
            "jsmith",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"], "janesmith")
    corpus = json.loads((directory / "corpus.json").read_text())
    assert corpus, "the feed declared by the blog in the GitHub bio was not read"
    assert {d["attribution"] for d in corpus} == {"linked"}
    assert all(d["attribution_basis"] for d in corpus)

    discovery = json.loads((directory / "discovery.json").read_text())
    assert discovery["card"]["name"] == "Jane Smith"
    assert discovery["searches"] == 0
    assert any("feed.xml" in c["url"] for c in discovery["candidates"])


def test_a_name_match_is_recorded_and_not_ingested(wired) -> None:
    """The failure mode that matters most, checked through the CLI: a stranger's
    blog reached from a README is written down, not read."""
    seed(
        wired,
        {
            "get:https://api.github.com/users/jsmith": json.dumps({"blog": ""}),
            "get:https://raw.githubusercontent.com/jsmith/jsmith/HEAD/README.md": (
                "Great post by someone else: https://unrelated-person.example/post"
            ),
        },
    )
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(3)}, source="rss")
    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--github",
            "jsmith",
            "--rss",
            "https://janesmith.com/feed.xml",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
        ]
    )
    assert "matched the name and nothing else" in result.output

    directory = outputs(wired["out"], "janesmith")
    corpus = json.loads((directory / "corpus.json").read_text())
    assert not any("unrelated-person" in d["url"] for d in corpus)

    discovery = json.loads((directory / "discovery.json").read_text())
    held = discovery["held"]
    assert any("unrelated-person" in h["url"] for h in held)
    assert held[0]["missing"], "a held candidate must say what did not match"
    report = (directory / "report.md").read_text()
    assert "matched the name and nothing else" in report
    assert "listed in discovery.json" in report


def test_no_discover_still_reads_the_anchors(wired) -> None:
    """Two different decisions: 'do not crawl' is not 'do not read what I gave
    you'."""
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(3)}, source="rss")
    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--profiles",
            str(wired["profiles"]),
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            "--no-discover",
            "--rss",
            "https://janesmith.com/feed.xml",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "--no-discover" in result.output
    corpus = json.loads((outputs(wired["out"], "janesmith") / "corpus.json").read_text())
    assert len(corpus) == 3
    assert {d["attribution"] for d in corpus} == {"anchor"}


def test_dry_run_prints_the_plan_and_the_phase_split(wired) -> None:
    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--github",
            "jsmith",
            "--out",
            str(wired["out"]),
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "api.github.com/users/jsmith" in result.output
    for phase in (
        "discovery (plain HTTP)",
        "fetch — X data",
        "search (phase 2)",
        "map:",
        "reduce:",
    ):
        assert phase in result.output
    # The queries are named and none of them is run: --dry-run promises to stop
    # before any paid fetch, and a search is a paid fetch.
    assert "none of them run" in result.output
    assert '"Jane Smith" blog OR essay OR writing' in result.output
    assert "spent so far: $0.0000" in result.output
    assert not (wired["out"]).exists() or not any((wired["out"]).rglob("corpus.json"))


def test_no_search_plans_no_searches(wired) -> None:
    result = invoke(
        [
            "run",
            "--name",
            "Jane Smith",
            "--github",
            "jsmith",
            "--out",
            str(wired["out"]),
            "--no-search",
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "0 searches (--no-search)" in result.output
    assert "search (phase 2):        ~$0.000" in result.output


def test_a_target_run_uses_the_saved_card(wired) -> None:
    invoke(
        [
            "profile",
            "--name",
            "Jane Smith",
            "--employer",
            "Acme Corp",
            "--rss",
            "https://janesmith.com/feed.xml",
        ]
    )
    seed(wired, {"rss:https://janesmith.com/feed.xml": feed(4)}, source="rss")
    result = invoke(
        [
            "run",
            "--target",
            "janesmith",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
        ]
    )
    assert result.exit_code == 0, result.output
    corpus = json.loads((outputs(wired["out"], "janesmith") / "corpus.json").read_text())
    assert len(corpus) == 4


def test_an_unknown_target_fails_before_anything_is_fetched(wired) -> None:
    result = invoke(["run", "--target", "nobody", "--out", str(wired["out"]), "--yes"])
    assert result.exit_code == 2
    assert "nobody" in result.output


def test_a_github_anchor_produces_documents_and_states_its_limits(wired) -> None:
    """The whole point of the GitHub adapter, end to end: a person with no X
    presence, read from their review and issue comments — and a report that
    says what the events feed could not reach."""
    events = json.loads((Path(__file__).parent / "fixtures" / "github_events.json").read_text())
    seed(
        wired,
        {"https://api.github.com/users/testsubject/events/public?per_page=100&page=1": events},
        source="github",
    )
    seed(
        wired,
        {"https://api.github.com/users/testsubject/events/public?per_page=100&page=2": []},
        source="github",
    )
    # The GitHub profile discovery reads, so link-following finds nothing else.
    seed(wired, {"get:https://api.github.com/users/testsubject": json.dumps({"blog": ""})})
    seed(
        wired,
        {"get:https://raw.githubusercontent.com/testsubject/testsubject/HEAD/README.md": "hi"},
    )

    result = invoke(
        [
            "run",
            "--name",
            "Test Subject",
            "--github",
            "testsubject",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
        ]
    )
    assert result.exit_code == 0, result.output

    directory = outputs(wired["out"], "testsubject")
    corpus = json.loads((directory / "corpus.json").read_text())
    assert corpus, "the GitHub anchor produced no documents"
    assert {d["source"] for d in corpus} == {"github"}
    kinds = {d["raw"]["kind"] for d in corpus}
    assert kinds == {"review_comment", "issue_comment"}
    assert {d["attribution"] for d in corpus} == {"anchor"}

    report = (directory / "report.md").read_text()
    assert "90 days" in report and "300 events" in report, (
        "the events-feed ceiling has to reach the coverage block; a reader who "
        "infers it from a short date range infers something about the person"
    )
    assert "carried no commit messages" in report


def test_the_x_only_path_is_unchanged(wired) -> None:
    """The regression that matters for everyone already using this: an X handle
    with no card still runs, and its documents are still anchor-attributed."""
    result = invoke(
        [
            "run",
            "--x",
            "testsubject",
            "--budget",
            "5",
            "--out",
            str(wired["out"]),
            "--yes",
            "--skip-synthesis",
            "--window-days",
            "60",
            "--empty-window-tolerance",
            "30",
        ]
    )
    assert result.exit_code == 0, result.output
    directory = outputs(wired["out"], "testsubject")
    corpus = json.loads((directory / "corpus.json").read_text())
    assert corpus
    assert {d["attribution"] for d in corpus} == {"anchor"}
    report = (directory / "report.md").read_text()
    assert report.startswith("# @testsubject — how they think")
    assert "Attribution:" not in report, (
        "an all-anchor corpus needs no attribution line; printing one on every "
        "report trains the reader to skip the one that matters"
    )
