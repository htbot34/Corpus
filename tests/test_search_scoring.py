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

from corpus.identity import IdentityCard, build_card
from corpus.models import ATTRIBUTION_CONFIDENCE
from corpus.search.pagefacts import extract_facts, facts_from_snippet, name_matches
from corpus.search.scoring import (
    CORROBORATION_THRESHOLD,
    MODERATE,
    STRONG,
    WEAK,
    corroboration_points,
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


def test_one_moderate_signal_is_not_corroboration() -> None:
    facts = extract_facts(
        page(body="A post that happens to mention Acme Corp."),
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card())

    assert score.points < CORROBORATION_THRESHOLD
    assert score.outcome == "held"
    assert not score.ingestible


def test_a_weak_signal_is_half_a_point_and_never_promotes_alone() -> None:
    """Location is real evidence and weak evidence. A weak fact about a
    common name is a coincidence, and half a point says exactly that."""
    facts = extract_facts(
        page(body="Written in Seattle, where lots of people live."),
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card(location="Seattle"))

    assert names(score.signals) == {"location"}
    assert [s.weight for s in score.signals] == [WEAK]
    assert score.points == 0.5
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

    assert score.points >= CORROBORATION_THRESHOLD, (
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

    assert score.points >= CORROBORATION_THRESHOLD
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
    same byline rendered twice. Scored separately they would be worth 3.0
    points — a strong plus a moderate — manufactured out of a single claim.
    The group counts once, at its strongest member's weight: this page is
    exactly one self-declared author, 2.0 points, no more."""
    facts = extract_facts(
        page(author="Jane Smith", body="By Jane Smith\n\nA post about rubrics."),
        "https://unrelated.example/post",
    )
    score = score_candidate(facts, card())

    assert count_independent(score.signals) <= 1
    assert corroboration_points(score.signals) == 2.0
    assert score.outcome == "corroborated"


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


# -- weight, not count -------------------------------------------------------
#
# The dustinw run: `links_to_anchor` — a strong signal — fired six times and
# only two candidates corroborated, because the threshold counted signals
# instead of weighing them. Two moderate agreements promoted; one strong
# self-declaration did not. These tests pin the weighted threshold and the
# placement judgement that makes a lone strong link safe to promote on.


def test_two_moderate_signals_still_promote() -> None:
    facts = extract_facts(
        page(body="Jane Smith is VP Engineering at Acme Corp."),
        "https://thinking.example/post",
    )
    score = score_candidate(facts, card(role="VP Engineering"))

    assert score.points == CORROBORATION_THRESHOLD
    assert score.outcome == "corroborated"


def test_one_strong_signal_from_the_pages_own_furniture_promotes() -> None:
    """The fix, from the passing side: a byline-block link to their GitHub is
    the page identifying itself, worth 2.0 points on its own."""
    facts = extract_facts(
        "<html><body><p>Jane Smith on rubrics, and why the rubric comes first.</p>"
        '<div class="post-author">By <a href="https://github.com/jsmith">jsmith</a></div>'
        "</body></html>",
        "https://thinking.example/post",
    )
    score = score_candidate(facts, card())

    assert [s.weight for s in score.signals if s.name == "links_to_anchor"] == [STRONG]
    assert score.points == CORROBORATION_THRESHOLD
    assert score.outcome == "corroborated"
    assert score.ingestible


def test_a_strangers_page_that_links_their_github_is_not_ingested() -> None:
    """The judgement call in the links_to_anchor split, from the failing side.

    A page ABOUT the target — no author metadata, one name mention, no
    third-person speech verbs, nothing for the about-detector to catch — that
    links their GitHub because it is discussing them. At a flat strong weight
    this promoted on the link alone, and a stranger's blog post entered the
    corpus at full corroborated confidence.
    """
    facts = extract_facts(
        page(
            body="Jane Smith wrote a lovely essay on rubrics this month.",
            links=["https://github.com/jsmith"],
        ),
        "https://someone-else.example/monthly-links",
    )
    score = score_candidate(facts, card())

    assert score.name_present
    assert [s.weight for s in score.signals if s.name == "links_to_anchor"] == [MODERATE]
    assert score.outcome == "held"
    assert not score.ingestible


def test_a_page_that_declares_the_handle_makes_the_anchor_link_strong() -> None:
    """A single strong signal from a page that also declares the handle as its
    own is a self-identification, and it is ingested."""
    facts = extract_facts(
        page(
            body="Jane Smith on rubrics, and why the rubric comes first.",
            links=["https://github.com/jsmith"],
            handle="jsmith",
        ),
        "https://thinking.example/notes",
    )
    score = score_candidate(facts, card())

    assert [s.weight for s in score.signals if s.name == "links_to_anchor"] == [STRONG]
    assert score.outcome == "corroborated"
    assert score.ingestible


def test_about_not_by_short_circuits_the_single_strong_signal_path() -> None:
    """Every promotion path, including the new one, sits behind the negatives.
    An interview page with a byline-block link to their GitHub is still a
    page about them."""
    facts = extract_facts(
        "<html><head><title>Interview with Jane Smith</title></head>"
        "<body><p>Jane Smith of Acme Corp answers our questions.</p>"
        '<div class="post-author"><a href="https://github.com/jsmith">her GitHub</a></div>'
        "</body></html>",
        "https://podcast.example/episodes/42",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "context"
    assert not score.ingestible
    assert score.negatives[0].name == "about_not_by"


def test_citation_only_short_circuits_the_single_strong_signal_path() -> None:
    facts = extract_facts(
        "<html><body><p>A wonderful piece, via Jane Smith.</p>"
        '<div class="post-author"><a href="https://github.com/jsmith">source</a></div>'
        "</body></html>",
        "https://someone-else.example/links",
    )
    score = score_candidate(facts, card())

    assert score.outcome == "rejected"
    assert not score.ingestible
    assert score.negatives[0].name == "citation_only"


def test_declared_links_are_the_pages_furniture_not_its_prose() -> None:
    """The extraction the placement judgement rests on: links in author and
    byline blocks and in <address> are kept apart from links in body prose —
    and a footer is body prose, because blogrolls live there."""
    facts = extract_facts(
        '<html><body><div class="byline">By <a href="https://github.com/jsmith">jsmith</a></div>'
        '<p>See also <a href="https://github.com/other">someone else</a>.</p>'
        '<address><a href="https://janesmith.com">home</a></address>'
        '<footer><a href="https://blogroll.example/friend">a friend</a></footer></body></html>',
        "https://thinking.example/post",
    )

    assert facts.declared_links == ["https://github.com/jsmith", "https://janesmith.com"]
    assert "https://github.com/other" in facts.links
    assert "https://blogroll.example/friend" in facts.links


BLOGROLL_PAGE = """<html><body><article><h1>Notes on variational inference</h1>
<p>Dustin Tran's work on Edward is the obvious starting point, and I want to
write down what I took from it. The rest is my own confusion, worked through
slowly, with no particular authority behind it.</p></article>
<footer><h3>Friends and people I read</h3><ul>
<li><a href="https://dustintran.com">Dustin Tran</a></li>
<li><a href="https://github.com/dustinvtran">his github</a></li>
<li><a href="https://example.org/someone">Someone Else</a></li>
</ul></footer></body></html>"""


def blogroll_card() -> IdentityCard:
    return build_card(
        name="Dustin Tran",
        key="dustinw",
        x="dustinvtran",
        github="dustinvtran",
        site="https://dustintran.com",
    )


def test_a_strangers_footer_blogroll_must_not_ingest_the_page() -> None:
    """The live reproduction, verbatim: a stranger's essay whose footer
    blogroll ("friends and people I read") links the target's homepage and
    GitHub. When <footer> counted as the page's own furniture, those links
    scored strong, the page corroborated at 2.0 points, and someone else's
    confusion entered the corpus as the target's writing at 0.6 confidence.
    A footer is where blogrolls live; it is never self-identification."""
    facts = extract_facts(BLOGROLL_PAGE, "https://randomblog.example/vi-notes")
    score = score_candidate(facts, blogroll_card())

    assert score.outcome == "held"
    assert not score.ingestible


def test_a_bigger_footer_blogroll_is_still_not_self_identification() -> None:
    """Same shape, five footer links to five different hosts, one of them the
    target's. More company in the blogroll must not change the verdict."""
    links = "".join(
        f'<li><a href="{href}">someone</a></li>'
        for href in (
            "https://dustintran.com",
            "https://alice.example",
            "https://bob.example/blog",
            "https://carol.example",
            "https://dave.example/notes",
        )
    )
    page_html = BLOGROLL_PAGE.replace(
        '<li><a href="https://dustintran.com">Dustin Tran</a></li>\n'
        '<li><a href="https://github.com/dustinvtran">his github</a></li>\n'
        '<li><a href="https://example.org/someone">Someone Else</a></li>',
        links,
    )
    assert 'href="https://alice.example"' in page_html, "the replace must have applied"
    facts = extract_facts(page_html, "https://randomblog.example/vi-notes")
    score = score_candidate(facts, blogroll_card())

    assert score.outcome == "held"
    assert not score.ingestible


def test_a_wordpress_author_body_class_does_not_make_the_page_furniture() -> None:
    """WordPress stamps `author author-<slug>` onto <body> on author archive
    pages, and themes add `single-author` sitewide. If <body> could be
    furniture, every prose link on half the blogs on the internet would be a
    declaration — including a stranger's inline link to the target's GitHub,
    which would then promote on its own."""
    facts = extract_facts(
        '<html><body class="archive category single-author author author-bob">'
        "<p>Jane Smith wrote a lovely essay on rubrics this month. See "
        '<a href="https://github.com/jsmith">her GitHub</a>.</p></body></html>',
        "https://someone-else.example/weekly-links",
    )
    score = score_candidate(facts, card())

    assert facts.declared_links == []
    assert [s.weight for s in score.signals if s.name == "links_to_anchor"] == [MODERATE]
    assert score.outcome == "held"
    assert not score.ingestible


def test_an_unclosed_author_block_does_not_swallow_the_rest_of_the_page() -> None:
    """A furniture element must actually close before its links count. Left
    open, a `<div class="author">` would claim every later link on the page
    — and real pages leave elements open constantly."""
    facts = extract_facts(
        '<html><body><div class="author"><span>Bob</span>'
        "<p>Jane Smith wrote a lovely essay on rubrics this month.</p>"
        '<a href="https://github.com/jsmith">link</a></body></html>',
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card())

    assert facts.declared_links == []
    assert score.outcome == "held"
    assert not score.ingestible


def test_an_oversized_author_classed_wrapper_is_not_a_byline() -> None:
    """A block wearing an author class around the whole article is a wrapper,
    not a byline; a real byline runs a line or three."""
    prose = "Jane Smith wrote a lovely essay on rubrics this month. " * 12
    facts = extract_facts(
        f'<html><body><div class="author-page"><p>{prose}</p>'
        '<a href="https://github.com/jsmith">her GitHub</a></div></body></html>',
        "https://someone-else.example/post",
    )
    score = score_candidate(facts, card())

    assert facts.declared_links == []
    assert score.outcome == "held"


def test_furniture_classes_match_on_tokens_not_substrings() -> None:
    """ "authorization-notice" is not a byline. The words themselves have to
    appear, so "post-author" and "byline__name" match and "authoritative"
    does not."""
    facts = extract_facts(
        '<html><body><div class="authorization-notice">'
        'Sign in to comment. <a href="https://github.com/jsmith">login</a></div>'
        '<div class="post-author">By <a href="https://janesmith.com">Jane</a></div>'
        "</body></html>",
        "https://someone-else.example/post",
    )

    assert facts.declared_links == ["https://janesmith.com"]


def test_the_handle_declaration_is_one_fact_not_two() -> None:
    """When the declared handle is what makes the anchor link strong, the
    same declaration must not also fire `declared_handle`: 2.0 points, not
    3.0, for one link plus one declaration."""
    facts = extract_facts(
        page(
            body="Jane Smith on rubrics, and why the rubric comes first.",
            links=["https://github.com/jsmith"],
            handle="jsmith",
        ),
        "https://thinking.example/notes",
    )
    score = score_candidate(facts, card())

    assert "declared_handle" not in names(score.signals)
    assert score.points == 2.0


def test_corroborated_confidence_is_capped_inside_its_tier() -> None:
    """A page can stack every signal there is; confidence still stops at 0.8,
    under linked's 0.85. Very corroborated is not linked."""
    facts = extract_facts(
        page(
            author="Jane Smith",
            body=(
                "Jane Smith, VP Engineering at Acme Corp, in Seattle. Find me at @jsmith on GitHub."
            ),
            links=["https://github.com/jsmith"],
            handle="janesmith",
        ),
        "https://thinking.example/essay",
    )
    subject = card(
        role="VP Engineering",
        location="Seattle",
        anchors={"github": "jsmith", "x": "janesmith"},
    )
    score = score_candidate(facts, subject)

    assert score.outcome == "corroborated"
    assert score.points > CORROBORATION_THRESHOLD + 2
    assert score.confidence == 0.8
    assert score.confidence < ATTRIBUTION_CONFIDENCE["linked"]


# -- the identity precondition ----------------------------------------------
#
# The arao run: three documents ingested as the target's own writing, two of
# them interviews with Aravind Srinivas and one with Arvind Narayanan — the
# target was Aravind Rao. Employer (moderate, 1.0) plus role (moderate, 1.0)
# reached the 2.0 threshold with no name match required, so two generic facts
# outweighed the identity they were supposed to corroborate. A name match is a
# precondition for corroboration, not one signal among several.


def arao_card() -> IdentityCard:
    return build_card(
        name="Aravind Rao",
        key="arao",
        x="aravrao",
        employer="OpenAI",
        role="Member of Technical Staff",
    )


def test_a_page_naming_nobody_cannot_be_corroborated_by_generic_facts() -> None:
    """The live reproduction, verbatim: a sentence about nobody at all reached
    `corroborated` at 2.0 points on employer plus role."""
    facts = extract_facts(
        page(body="The company OpenAI hires many a Member of Technical Staff each year."),
        "https://jobs.example/openai-hiring",
    )
    score = score_candidate(facts, arao_card())

    assert not score.name_present
    assert score.points >= CORROBORATION_THRESHOLD, (
        "the generic facts must still reach the threshold, or the precondition "
        "is not what explains the held outcome"
    )
    assert not score.identity_established
    assert score.outcome == "held"
    assert not score.ingestible


def test_a_page_about_a_different_person_with_matching_facts_is_never_ingested() -> None:
    facts = extract_facts(
        page(body="Aravind Srinivas is a Member of Technical Staff at OpenAI."),
        "https://blog.example/profile",
    )
    score = score_candidate(facts, arao_card())

    assert score.outcome in ("held", "rejected", "context")
    assert not score.ingestible


def test_a_page_naming_the_target_with_matching_facts_still_corroborates() -> None:
    """The precondition must not eat the legitimate case: name plus employer
    plus role is exactly what corroboration is."""
    facts = extract_facts(
        page(body="Aravind Rao is a Member of Technical Staff at OpenAI."),
        "https://thinking.example/post",
    )
    score = score_candidate(facts, arao_card())

    assert score.name_present
    assert score.identity_established
    assert score.outcome == "corroborated"
    assert score.ingestible


def test_the_lexfridman_transcript_is_not_the_targets_writing() -> None:
    """The first live misattribution: an interview transcript with Aravind
    Srinivas, ingested as Aravind Rao's own writing because OpenAI and the
    role were on the page."""
    facts = extract_facts(
        page(
            title="Aravind Srinivas: Perplexity, and leaving OpenAI",
            body=(
                "Aravind Srinivas was a Member of Technical Staff at OpenAI before "
                "founding Perplexity. The full transcript of our conversation follows."
            ),
        ),
        "https://lexfridman.com/aravind-srinivas-transcript",
    )
    score = score_candidate(facts, arao_card())

    assert score.outcome != "corroborated"
    assert not score.ingestible


def test_the_interconnects_interview_is_not_the_targets_writing() -> None:
    """The second live misattribution — a different person entirely (Arvind
    Narayanan), on a page whose only agreement with the card was generic."""
    facts = extract_facts(
        page(
            title="Interviewing Arvind Narayanan",
            body=(
                "Arvind Narayanan on AI snake oil, agents, and evaluation. OpenAI's "
                "Member of Technical Staff title comes up more than once."
            ),
        ),
        "https://www.interconnects.ai/p/interviewing-arvind-narayanan",
    )
    score = score_candidate(facts, arao_card())

    assert score.outcome != "corroborated"
    assert not score.ingestible


def test_the_held_page_says_the_identity_was_never_established() -> None:
    """unconfirmed.md must tell the human why 2.0 points did not promote."""
    facts = extract_facts(
        page(body="The company OpenAI hires many a Member of Technical Staff each year."),
        "https://jobs.example/openai-hiring",
    )
    score = score_candidate(facts, arao_card())

    assert any("never attaches their name" in m for m in score.missing)


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
