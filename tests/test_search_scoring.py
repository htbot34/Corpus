"""The scoring model: does this page belong to the target?

Every test here is a pure function call. No network, no model, no cache, no
clock — which is the point. This is the code that decides whether a stranger's
essay is attributed to the subject, and it must be arguable on a plane.

The tests are organised around the failure this whole phase exists to prevent,
so the file reads as a list of ways to get the wrong person and the reason each
one is caught.
"""

from __future__ import annotations

import pytest

from corpus.identity import IdentityCard
from corpus.search.pagefacts import extract_facts, facts_from_snippet, name_matches
from corpus.search.scoring import (
    CORROBORATION_THRESHOLD,
    MODERATE,
    STRONG,
    WEAK,
    count_independent,
    score_candidate,
)


def card(**kwargs: object) -> IdentityCard:
    base: dict[str, object] = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "anchors": {"github": "jsmith", "site": "https://janesmith.com"},
    }
    base.update(kwargs)
    return IdentityCard(**base)  # type: ignore[arg-type]


def page(
    *,
    author: str = "",
    body: str = "",
    title: str = "",
    links: list[str] | None = None,
    handle: str = "",
) -> str:
    """A minimal HTML page carrying whatever the test is about."""
    head = [f"<title>{title}</title>"] if title else []
    if author:
        head.append(f'<meta name="author" content="{author}">')
    if handle:
        head.append(f'<meta name="twitter:creator" content="@{handle}">')
    anchors = "".join(f'<a href="{href}">link</a>' for href in (links or []))
    return f"<html><head>{''.join(head)}</head><body><p>{body}</p>{anchors}</body></html>"


def names(signals: list[object]) -> set[str]:
    return {s.name for s in signals}


# -- the clean case ---------------------------------------------------------


def test_a_page_that_agrees_with_the_card_twice_over_is_corroborated() -> None:
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Notes on rubrics, written while at Acme Corp.",
            links=["https://github.com/jsmith"],
        ),
        "https://thinking.example/rubrics",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "corroborated"
    assert score.ingestible
    assert {"author_metadata", "links_to_anchor", "employer"} <= names(score.signals)


def test_search_can_never_promote_to_linked() -> None:
    """`linked` means "reached from a declared field on an anchor".

    Search is not that, and no quantity of search evidence turns into a
    self-declaration. This is a rule about what the tiers *mean*, so it is
    pinned rather than left to the reader of the scoring code.
    """
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Jane Smith, VP Engineering at Acme Corp, in Seattle.",
            links=["https://github.com/jsmith", "https://janesmith.com"],
            handle="janesmith",
        ),
        "https://thinking.example/essay",
    )
    score = score_candidate(facts, card(role="VP Engineering", location="Seattle"))

    assert score.outcome == "corroborated"
    assert score.attribution == "corroborated"
    assert score.confidence < 0.85, "a search find must never reach linked's confidence"


def test_one_signal_is_not_corroboration() -> None:
    facts = extract_facts(
        page(body="A post that happens to mention Acme Corp."),
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card())

    assert score.independent_count < CORROBORATION_THRESHOLD
    assert score.outcome == "held"
    assert not score.ingestible


def test_a_weak_signal_does_not_count_toward_the_threshold() -> None:
    """Location is real evidence and weak evidence. Two weak facts about a
    common name are not a match; they are a coincidence with two parts."""
    facts = extract_facts(
        page(body="Written in Seattle, where lots of people live."),
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card(location="Seattle"))

    assert names(score.signals) == {"location"}
    assert [s.weight for s in score.signals] == [WEAK]
    assert score.outcome == "held"


# -- the single most important test in the suite ----------------------------


def test_a_name_match_is_never_ingested() -> None:
    """A page that matches the name and nothing else must never enter a corpus.

    If this test ever goes green while `ingestible` is True, the tool has
    become the thing it exists not to be: a confident report indistinguishable
    from a correct one.
    """
    facts = extract_facts(
        page(body="An essay by some Jane Smith, somewhere on the internet."),
        "https://unrelated.example/essay",
    )
    score = score_candidate(facts, card())

    assert score.name_present, "the fixture is pointless if the name does not match"
    assert score.attribution == "name_match"
    assert score.outcome == "held"
    assert not score.ingestible


# -- negatives demote, they do not merely fail to promote -------------------


def test_a_different_employer_demotes_a_page_that_otherwise_matched() -> None:
    """Two strong signals, one contradiction, and the contradiction wins.

    This is the difference between a scorer with negative signals and one
    without: without them, this page is corroborated on the strength of a
    byline and a link, and a different Jane Smith's essay enters the corpus.
    """
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Jane Smith is a partner at Beta Industries and writes about tax law.",
            links=["https://github.com/jsmith"],
        ),
        "https://taxblog.example/post",
    )
    score = score_candidate(facts, card())

    assert score.independent_count >= CORROBORATION_THRESHOLD, (
        "the positives must be strong enough that only the negative can explain the demotion"
    )
    assert "different_employer" in {n.name for n in score.negatives}
    assert score.outcome == "held"
    assert not score.ingestible


def test_naming_the_right_employer_resolves_the_contradiction() -> None:
    """A page mentioning two companies is a page mentioning two companies."""
    facts = extract_facts(
        page(
            author="Jane Smith",
            body=(
                "Jane Smith is a partner at Beta Industries. She previously worked at Acme Corp."
            ),
            links=["https://github.com/jsmith"],
        ),
        "https://thinking.example/post",
    )
    score = score_candidate(facts, card())

    assert not score.negatives
    assert score.outcome == "corroborated"


def test_a_different_field_demotes_on_its_own() -> None:
    """The employer is deliberately *correct* here, so the only thing that can
    explain the demotion is the field mismatch."""
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Jane Smith is a dermatology resident at Acme Corp.",
            links=["https://github.com/jsmith"],
        ),
        "https://clinic.example/staff-notes",
    )
    score = score_candidate(facts, card(role="VP Engineering"))

    assert score.independent_count >= CORROBORATION_THRESHOLD
    assert [n.name for n in score.negatives] == ["different_field"]
    assert score.outcome == "held"


def test_a_different_location_demotes() -> None:
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Jane Smith is based in Lisbon and works at Acme Corp.",
            links=["https://github.com/jsmith"],
        ),
        "https://thinking.example/post",
    )
    score = score_candidate(facts, card(location="Seattle"))

    assert "different_location" in {n.name for n in score.negatives}
    assert score.outcome == "held"


# -- rejected outright ------------------------------------------------------


def test_an_excluded_url_is_rejected_before_anything_else_is_considered() -> None:
    subject = card(exclude=["https://taxblog.example"])
    facts = extract_facts(
        page(author="Jane Smith", body="At Acme Corp.", links=["https://github.com/jsmith"]),
        "https://taxblog.example/post",
    )
    score = score_candidate(facts, subject)

    assert score.outcome == "rejected"
    assert not score.ingestible
    assert score.negatives[0].name == "excluded"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.crunchbase.com/person/jane-smith", "directory"),
        ("https://rocketreach.co/jane-smith-email_1234", "people_search"),
        ("https://www.zoominfo.com/p/Jane-Smith/999", "people_search"),
        ("https://muckrack.com/jane-smith", "directory"),
        ("https://www.linkedin.com/in/janesmith", "refused_platform"),
    ],
)
def test_aggregators_and_data_brokers_are_rejected(url: str, expected: str) -> None:
    """Not a quality judgement — a scope boundary.

    This tool reads what a person chose to publish under their own name.
    People-search output is not that, and a search engine will happily return
    it, so the refusal has to live where the results arrive.
    """
    facts = extract_facts(page(body="Jane Smith, Acme Corp, Seattle"), url)
    score = score_candidate(facts, card())

    assert score.outcome == "rejected"
    assert score.negatives[0].name == expected


def test_a_citation_of_their_work_is_not_a_source_of_it() -> None:
    facts = extract_facts(
        page(body="A wonderful piece on rubrics, via Jane Smith, worth your time."),
        "https://someone-else.example/links",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "rejected"
    assert score.negatives[0].name == "citation_only"


def test_one_citation_among_other_mentions_is_not_citation_only() -> None:
    facts = extract_facts(
        page(
            author="Jane Smith",
            body="Found via Jane Smith. Jane Smith also works at Acme Corp on rubrics.",
            links=["https://github.com/jsmith"],
        ),
        "https://thinking.example/post",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "corroborated"


# -- about them, not by them ------------------------------------------------


def test_a_profile_piece_is_context_and_never_a_corpus_document() -> None:
    """The failure that matters more than it looks.

    An interview write-up is *about* the target, reads as highly relevant to
    any name-matching scorer, and contains the interviewer's prose rather than
    the subject's reasoning. Ingesting it would put someone else's sentences
    in the report under the subject's name.
    """
    facts = extract_facts(
        page(
            author="Bob Reporter",
            title="A morning with Jane Smith",
            body="Jane Smith, of Acme Corp, met me at a cafe.",
        ),
        "https://magazine.example/features/jane",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "context"
    assert not score.ingestible
    assert score.negatives[0].name == "about_not_by"


def test_an_interview_url_is_read_as_a_page_about_them() -> None:
    facts = extract_facts(
        page(body="Jane Smith of Acme Corp answers our questions."),
        "https://podcast.example/interview-with-jane-smith",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "context"


def test_third_person_quotation_marks_a_page_as_about_them() -> None:
    facts = extract_facts(
        page(
            body=(
                "Jane Smith said the rubric came first. Jane Smith told us the "
                "team disagreed at the time."
            )
        ),
        "https://news.example/story",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "context"


def test_their_own_about_page_is_not_mistaken_for_a_profile_of_them() -> None:
    """The `/about` page on someone's own site is written by them, and is one
    of the better documents in any corpus. The about-marker rule must not eat
    it."""
    facts = extract_facts(
        page(body="About Jane Smith. I work at Acme Corp and write about rubrics."),
        "https://janesmith.com/about",
    )
    score = score_candidate(facts, card())

    assert score.outcome != "context"
    assert "anchor_domain" in names(score.signals)


# -- signal independence ----------------------------------------------------


def test_metadata_and_byline_are_one_claim_not_two() -> None:
    """`<meta name="author">` and a visible "By Jane Smith" are usually the
    same byline rendered twice. Counting both would manufacture corroboration
    out of a single claim, which is exactly how a threshold of two gets
    defeated."""
    facts = extract_facts(
        page(author="Jane Smith", body="By Jane Smith\n\nA post about rubrics."),
        "https://unrelated.example/post",
    )
    score = score_candidate(facts, card())

    assert count_independent(score.signals) <= 1
    assert score.outcome == "held"


def test_two_anchor_handles_on_one_page_is_a_strong_signal() -> None:
    facts = extract_facts(
        page(
            body="Find me at @jsmith on GitHub and elsewhere.",
            handle="janesmith",
        ),
        "https://thinking.example/colophon",
    )
    subject = card(anchors={"github": "jsmith", "x": "janesmith"})
    score = score_candidate(facts, subject)

    assert "two_handles" in names(score.signals)
    assert [s.weight for s in score.signals if s.name == "two_handles"] == [STRONG]


def test_an_employer_mention_is_moderate_not_strong() -> None:
    facts = extract_facts(page(body="Acme Corp is hiring."), "https://jobs.example/acme")
    score = score_candidate(facts, card())

    assert [s.weight for s in score.signals] == [MODERATE]


# -- snippets are leads, never evidence -------------------------------------


def test_a_snippet_cannot_award_the_metadata_signal() -> None:
    """Author metadata lives in the page. A snippet that happens to contain
    the name is not a declaration of authorship by anybody."""
    facts = facts_from_snippet(
        "https://unrelated.example/post",
        title="Rubrics",
        snippet="By Jane Smith, of Acme Corp",
    )
    score = score_candidate(facts, card())

    assert not facts.fetched
    assert "author_metadata" not in names(score.signals)
    assert any("not fetched" in m for m in score.missing)


# -- name matching ----------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "target", "expected"),
    [
        ("Jane Smith", "Jane Smith", True),
        ("Jane A. Smith", "Jane Smith", True),
        ("Smith, Jane", "Jane Smith", True),
        ("jane smith", "Jane Smith", True),
        ("Jane Doe", "Jane Smith", False),
        ("Smith", "Jane Smith", False),
        ("Robert Smith", "Jane Smith", False),
        ("", "Jane Smith", False),
    ],
)
def test_name_matching_tolerates_initials_but_not_strangers(
    candidate: str, target: str, expected: bool
) -> None:
    assert name_matches(candidate, target) is expected
