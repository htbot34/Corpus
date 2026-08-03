"""Query generation from the identity card.

The rule this file exists to pin is the first one in `queries.py`: **never
search a bare name.** Everything else — the field skipping, the location
boost, the cap — is in service of it.
"""

from __future__ import annotations

from corpus.identity import IdentityCard
from corpus.search.queries import (
    DEFAULT_MAX_SEARCHES,
    generate_queries,
    missing_fields,
    sanitize,
)


def card(**kwargs: object) -> IdentityCard:
    base: dict[str, object] = {"key": "jane", "name": "Jane Smith"}
    base.update(kwargs)
    return IdentityCard(**base)  # type: ignore[arg-type]


def texts(subject: IdentityCard, **kwargs: int) -> list[str]:
    return [q.text for q in generate_queries(subject, **kwargs)]


# -- the rule ---------------------------------------------------------------


def test_a_bare_name_is_never_a_query() -> None:
    """The query most likely to return somebody else, and the one a person
    would type by hand. So it is the one query this module refuses to emit."""
    for subject in (
        card(),
        card(employer="Acme Corp"),
        card(anchors={"github": "jsmith"}),
        card(name="John Smith", location="Seattle"),
    ):
        assert '"Jane Smith"' not in texts(subject)
        assert '"John Smith"' not in texts(subject)
        for text in texts(subject):
            assert text.strip() != f'"{subject.name}"'


def test_a_query_whose_field_is_missing_is_skipped_not_emitted_empty() -> None:
    """`"Jane Smith" ""` is not a narrower search. It is the same search with a
    syntax error in it."""
    queries = texts(card())

    assert queries, "a name alone should still yield the writing sweeps"
    assert not any('""' in q for q in queries)
    assert not any(q.rstrip().endswith('"Jane Smith"') for q in queries)


def test_an_employer_buys_the_most_precise_query() -> None:
    with_employer = texts(card(employer="Acme Corp"))
    without = texts(card())

    assert '"Jane Smith" "Acme Corp"' in with_employer
    assert not any("Acme" in q for q in without)


def test_handles_are_searched_as_handles() -> None:
    """People cite each other by handle, and a handle is unique in a way
    "Jane Smith" is not."""
    queries = texts(card(anchors={"x": "janesmith", "github": "jsmith"}))

    assert '"@janesmith"' in queries
    assert "jsmith github" in queries


def test_location_is_appended_to_the_two_highest_yield_queries_only() -> None:
    """It is the cheapest disambiguator for a common name — and on a query for
    their conference talks it costs recall and buys nothing, because talks are
    not tagged with where someone lives."""
    queries = texts(card(employer="Acme Corp", location="Seattle"))
    boosted = [q for q in queries if "Seattle" in q]

    assert len(boosted) == 2
    assert '"Jane Smith" "Acme Corp" Seattle' in boosted
    assert any("blog OR essay OR writing" in q for q in boosted)
    assert not any("keynote" in q and "Seattle" in q for q in queries)


def test_max_searches_caps_the_run_and_keeps_the_precise_queries() -> None:
    """The cap truncates from the end, so the ordering is what decides which
    queries a small budget buys."""
    subject = card(employer="Acme Corp", role="VP Engineering", anchors={"github": "jsmith"})
    full = texts(subject)
    capped = texts(subject, max_searches=3)

    assert len(full) > 3
    assert len(capped) == 3
    assert capped == full[:3]
    assert '"Jane Smith" "Acme Corp"' in capped


def test_the_cap_can_be_zero() -> None:
    assert texts(card(employer="Acme Corp"), max_searches=0) == []


def test_queries_are_unique() -> None:
    queries = texts(card(employer="Acme Corp", role="Acme Corp"))
    assert len(queries) == len(set(queries))


def test_the_default_cap_is_reported_not_implied() -> None:
    assert DEFAULT_MAX_SEARCHES == 12


# -- untrusted input --------------------------------------------------------


def test_search_operators_in_a_card_field_cannot_change_what_a_query_means() -> None:
    """A name is user input, and search operators are meaningful. There is no
    escaping convention shared across vendors, so anything that could change
    the query's meaning is removed rather than escaped."""
    assert sanitize('Jane" OR site:evil.example') == "Jane evil.example"
    assert sanitize("Jane (Smith)") == "Jane Smith"
    assert '"' not in sanitize('a "b" c')

    queries = texts(card(name='Jane" Smith', employer="Acme OR Beta"))
    for text in queries:
        assert text.count('"') % 2 == 0, f"unbalanced quotes in {text!r}"
        assert "site:evil" not in text


# -- what to ask the user for -----------------------------------------------


def test_the_missing_fields_are_named_in_the_order_they_disambiguate() -> None:
    assert missing_fields(card()) == ["employer", "location", "role"]
    assert missing_fields(card(employer="Acme Corp")) == ["location", "role"]
    assert missing_fields(card(employer="A", location="B", role="C")) == []
