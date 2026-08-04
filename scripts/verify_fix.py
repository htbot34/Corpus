#!/usr/bin/env python3
"""Confirm the footer-blogroll fix on this machine. No arguments, no network.

    python scripts/verify_fix.py

Runs the two pages that used to be wrongly ingested — a stranger's blog post
whose footer blogroll links the target's homepage and GitHub — through the
real scorer, and says PASS or FAIL in plain English. Everything here is
offline and free: the scorer is pure Python over an HTML string.
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

CASES = [
    (
        "a stranger's post whose footer blogroll links the target's homepage and GitHub",
        BLOGROLL_PAGE,
    ),
    (
        "the same page with five footer links to five different sites, one of them the target's",
        BIGGER_BLOGROLL,
    ),
]


def main() -> int:
    if 'href="https://alice.example"' not in BIGGER_BLOGROLL:
        print("FAIL — the second test page did not build correctly; this script has a bug.")
        return 1

    card = build_card(
        name="Dustin Tran",
        key="dustinw",
        x="dustinvtran",
        github="dustinvtran",
        site="https://dustintran.com",
    )

    failures = 0
    for description, html in CASES:
        facts = extract_facts(html, "https://randomblog.example/vi-notes")
        score = score_candidate(facts, card)
        held = score.outcome == "held" and not score.ingestible
        print(f"  checking: {description}")
        if held:
            print("    ok — the page was held for human review, not ingested.")
        else:
            failures += 1
            print(
                f"    WRONG — the scorer said '{score.outcome}'"
                f"{' and would ingest it' if score.ingestible else ''}. "
                "A stranger's page must be held, never ingested."
            )

    print()
    if failures:
        print("FAIL — the footer-blogroll hole is still open on this machine.")
        print("A stranger's blog post can still enter a corpus as the target's own writing.")
        return 1
    print("PASS — the footer-blogroll hole is closed on this machine.")
    print("Both pages were held for a human to review instead of being ingested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
