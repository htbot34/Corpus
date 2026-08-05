#!/usr/bin/env python3
"""Confirm the misattribution fixes on this machine. No arguments, no network.

    python scripts/verify_fix.py

Runs the pages that used to be wrongly ingested through the real scorer and
says PASS or FAIL in plain English. Two holes, three regressions:

* The footer-blogroll hole (the dustinw run): a stranger's blog post whose
  footer blogroll links the target's homepage and GitHub scored the links as
  the page's own furniture and was ingested.
* The generic-facts hole (the arao run): employer plus role reached the 2.0
  threshold with no name match at all, so interview pages about Aravind
  Srinivas and Arvind Narayanan were ingested as Aravind Rao's own writing.
  A name match is a precondition for corroboration, not one signal among
  several.

Everything here is offline and free: the scorer is pure Python over an HTML
string.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.identity import build_card
from corpus.search.pagefacts import extract_facts
from corpus.search.scoring import score_candidate

BLOGROLL_PAGE = """<html><body><article><h1>Notes on variational inference</h1>
<p>Dustin Tran's work on Edward is the obvious starting point, and I want to
write down what I took from it. The rest is my own confusion, worked through
slowly, with no particular authority behind it.</p></article>
<footer><h3>Friends and people I read</h3><ul>
<li><a href="https://dustintran.com">Dustin Tran</a></li>
<li><a href="https://github.com/dustinvtran">his github</a></li>
<li><a href="https://example.org/someone">Someone Else</a></li>
</ul></footer></body></html>"""

BIGGER_BLOGROLL = BLOGROLL_PAGE.replace(
    '<li><a href="https://dustintran.com">Dustin Tran</a></li>\n'
    '<li><a href="https://github.com/dustinvtran">his github</a></li>\n'
    '<li><a href="https://example.org/someone">Someone Else</a></li>',
    "".join(
        f'<li><a href="{href}">someone</a></li>'
        for href in (
            "https://dustintran.com",
            "https://alice.example",
            "https://bob.example/blog",
            "https://carol.example",
            "https://dave.example/notes",
        )
    ),
)

GENERIC_FACTS_PAGE = (
    "<html><body><p>The company OpenAI hires many a Member of Technical Staff "
    "each year.</p></body></html>"
)

SRINIVAS_TRANSCRIPT = (
    "<html><head><title>Aravind Srinivas: Perplexity, and leaving OpenAI</title></head>"
    "<body><p>Aravind Srinivas was a Member of Technical Staff at OpenAI before "
    "founding Perplexity. The full transcript of our conversation follows.</p>"
    "</body></html>"
)

NARAYANAN_INTERVIEW = (
    "<html><head><title>Interviewing Arvind Narayanan</title></head>"
    "<body><p>Arvind Narayanan on AI snake oil, agents, and evaluation. OpenAI's "
    "Member of Technical Staff title comes up more than once.</p></body></html>"
)


def check(description: str, html: str, url: str, card) -> bool:  # type: ignore[no-untyped-def]
    facts = extract_facts(html, url)
    score = score_candidate(facts, card)
    safe = score.outcome != "corroborated" and not score.ingestible
    print(f"  checking: {description}")
    if safe:
        print(f"    ok — the page was {score.outcome}, not ingested.")
    else:
        print(
            f"    WRONG — the scorer said '{score.outcome}'"
            f"{' and would ingest it' if score.ingestible else ''}. "
            "A page that is not the target's writing must never be ingested."
        )
    return safe


def main() -> int:
    if 'href="https://alice.example"' not in BIGGER_BLOGROLL:
        print("FAIL — the second test page did not build correctly; this script has a bug.")
        return 1

    dustinw = build_card(
        name="Dustin Tran",
        key="dustinw",
        x="dustinvtran",
        github="dustinvtran",
        site="https://dustintran.com",
    )
    arao = build_card(
        name="Aravind Rao",
        key="arao",
        x="aravrao",
        employer="OpenAI",
        role="Member of Technical Staff",
    )

    failures = 0

    print("The footer-blogroll hole (dustinw):")
    for description, html in (
        (
            "a stranger's post whose footer blogroll links the target's homepage and GitHub",
            BLOGROLL_PAGE,
        ),
        (
            "the same page with five footer links to five different sites, one of them the target's",
            BIGGER_BLOGROLL,
        ),
    ):
        if not check(description, html, "https://randomblog.example/vi-notes", dustinw):
            failures += 1

    print()
    print("The generic-facts hole (arao):")
    for description, html, url in (
        (
            "a page naming nobody at all, matching employer and role",
            GENERIC_FACTS_PAGE,
            "https://jobs.example/openai-hiring",
        ),
        (
            "the Lex Fridman transcript: Aravind Srinivas, not Aravind Rao",
            SRINIVAS_TRANSCRIPT,
            "https://lexfridman.com/aravind-srinivas-transcript",
        ),
        (
            "the Interconnects interview: Arvind Narayanan, not Aravind Rao",
            NARAYANAN_INTERVIEW,
            "https://www.interconnects.ai/p/interviewing-arvind-narayanan",
        ),
    ):
        if not check(description, html, url, arao):
            failures += 1

    print()
    if failures:
        print("FAIL — a misattribution hole is still open on this machine.")
        print("A page that is not the target's writing can still enter their corpus.")
        return 1
    print("PASS — both holes are closed on this machine.")
    print(
        "The blogroll pages were held, and no page reached corroborated without "
        "the target's name or handle attached."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
