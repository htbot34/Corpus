"""`unconfirmed.md`: the round trip through a human.

The file is written by the tool, edited by a person in a text editor, and read
back by the tool. So the tests care about two things: that a reader can decide
from what is on the page, and that a hand-edited file survives being read back
however it was mangled on the way.
"""

from __future__ import annotations

from pathlib import Path

from corpus.identity import IdentityCard
from corpus.search.pagefacts import facts_from_snippet
from corpus.search.scoring import score_candidate
from corpus.search.unconfirmed import (
    read_unconfirmed,
    render_unconfirmed,
    write_unconfirmed,
)
from corpus.search.verify import SearchCandidate, SearchPhaseResult


def card(**kwargs: object) -> IdentityCard:
    base: dict[str, object] = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "anchors": {"github": "jsmith"},
    }
    base.update(kwargs)
    return IdentityCard(**base)  # type: ignore[arg-type]


def candidate(url: str, snippet: str = "A post by Jane Smith") -> SearchCandidate:
    subject = card()
    found = SearchCandidate(
        url=url,
        title="On rubrics",
        snippet=snippet,
        query='"Jane Smith" "Acme Corp"',
    )
    found.score = score_candidate(
        facts_from_snippet(url, found.title, snippet), subject, query=found.query
    )
    return found


def result_with(*urls: str) -> SearchPhaseResult:
    return SearchPhaseResult(held=[candidate(u) for u in urls])


# -- what a reader sees -----------------------------------------------------


def test_an_entry_says_what_found_it_what_matched_and_what_did_not() -> None:
    text = render_unconfirmed(card(), result_with("https://someblog.example/about"))

    assert "- [ ] https://someblog.example/about" in text
    assert "Found by: query `\"Jane Smith\" \"Acme Corp\"`" in text
    assert "Matched:" in text
    assert "Did not match:" in text
    assert "Snippet: A post by Jane Smith" in text


def test_the_file_says_what_ticking_a_box_will_do() -> None:
    """Unchecked means rejected forever, which is a surprising enough rule
    that the file has to state it where the person editing it will read it."""
    text = render_unconfirmed(card(), result_with("https://someblog.example/x"))

    assert "not** in the corpus" in text
    assert "corroborated" in text
    assert "exclude" in text
    assert "do not pass this file" in text


def test_pages_about_them_are_listed_apart_and_marked_never_ingested() -> None:
    result = SearchPhaseResult(
        held=[candidate("https://blog.example/post")],
        context=[candidate("https://magazine.example/interview-with-jane")],
    )
    text = render_unconfirmed(card(), result)

    assert "## About them, not by them" in text
    assert "never ingested" in text
    assert "https://magazine.example/interview-with-jane" in text
    # It is not offered as a checkbox: no box means no way to accept it.
    about_section = text.split("## About them, not by them", 1)[1]
    assert "- [ ]" not in about_section


def test_a_common_name_run_says_which_fields_would_help() -> None:
    result = result_with("https://site1.example/a")
    result.common_name = True
    result.disambiguators = ["employer", "location"]
    result.notes = ["9 distinct domains matched the name and nothing else."]

    text = render_unconfirmed(card(name="John Smith"), result)

    assert "too common to search on" in text
    assert "`employer`" in text and "`location`" in text
    assert "corpus profile --target jane" in text


def test_an_empty_run_still_writes_a_file_that_says_so(tmp_path: Path) -> None:
    path = write_unconfirmed(tmp_path, card(), SearchPhaseResult())

    assert path.name == "unconfirmed.md"
    assert "Nothing was held back" in path.read_text(encoding="utf-8")


# -- reading it back --------------------------------------------------------


def test_a_ticked_box_is_an_acceptance_and_an_empty_one_is_a_rejection(
    tmp_path: Path,
) -> None:
    path = write_unconfirmed(
        tmp_path, card(), result_with("https://yes.example/a", "https://no.example/b")
    )
    edited = path.read_text(encoding="utf-8").replace(
        "- [ ] https://yes.example/a", "- [x] https://yes.example/a"
    )
    path.write_text(edited, encoding="utf-8")

    entries = read_unconfirmed(path)

    assert {e.url: e.checked for e in entries} == {
        "https://yes.example/a": True,
        "https://no.example/b": False,
    }


def test_the_round_trip_carries_the_query_and_the_reasons(tmp_path: Path) -> None:
    path = write_unconfirmed(tmp_path, card(), result_with("https://someblog.example/about"))

    entry = read_unconfirmed(path)[0]

    assert entry.query == '"Jane Smith" "Acme Corp"'
    assert entry.missing, "a held candidate must say what did not match"


def test_a_hand_edited_file_survives_being_read_back(tmp_path: Path) -> None:
    """Written by a tool, edited by a person. Indentation, `*` bullets, a
    capital X, deleted context lines and added prose are all things a text
    editor produces, and none of them should lose a decision."""
    path = tmp_path / "unconfirmed.md"
    path.write_text(
        """# Unconfirmed

Some notes I typed myself.

  * [X] https://yes.example/a
        Found by: query `"Jane Smith"`

- [x]  https://also-yes.example/b
- [ ] https://no.example/c
      Matched: name in byline

- [ ] not a url at all
""",
        encoding="utf-8",
    )

    entries = read_unconfirmed(path)

    assert [(e.url, e.checked) for e in entries] == [
        ("https://yes.example/a", True),
        ("https://also-yes.example/b", True),
        ("https://no.example/c", False),
    ]


def test_an_untouched_file_is_recognisable_as_untouched(tmp_path: Path) -> None:
    """Unchecked means "exclude forever", so a file nobody edited would reject
    everything. The count of checked entries is how the CLI refuses to act on
    one."""
    path = write_unconfirmed(tmp_path, card(), result_with("https://a.example/1"))

    entries = read_unconfirmed(path)

    assert entries
    assert not any(e.checked for e in entries)


def test_the_same_url_twice_is_read_once(tmp_path: Path) -> None:
    path = tmp_path / "unconfirmed.md"
    path.write_text(
        "- [x] https://a.example/1\n\n- [ ] https://a.example/1\n",
        encoding="utf-8",
    )

    entries = read_unconfirmed(path)

    assert len(entries) == 1
    assert entries[0].checked, "the first, more considered decision wins"


def test_trailing_punctuation_is_not_part_of_the_url(tmp_path: Path) -> None:
    path = tmp_path / "unconfirmed.md"
    path.write_text("- [x] https://a.example/page.\n", encoding="utf-8")

    assert read_unconfirmed(path)[0].url == "https://a.example/page"
