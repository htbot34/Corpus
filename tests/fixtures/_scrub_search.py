"""Turn a live search capture into the fixture. Run once; kept as provenance.

PROVENANCE. `web_search_response.json` is a **real response**, captured from
the wire on 2026-08-03 by

    python scripts/verify_search_contract.py --target dustinw --capture-search captures/

which ran nine queries and wrote nine files. This script scrubs one of them —
the `blog OR essay OR writing` sweep, chosen because it returned the maximum
observed ten results and the widest spread of shapes: the subject's own site
over both http and https, two platform profiles, a video, a directory listing
for a different person of the same first name, and an encyclopedia article
about a different person entirely.

WHAT IS REAL, and is the entire point of the fixture:

* every key name, at every level of nesting, and the order blocks arrive in
* which keys are present and which are absent — including the finding that
  drove this pass: `citations` is **null** on the text block, so a search
  result's snippet is always empty (see below)
* `caller`, `container`, `stop_details`, `output_tokens_details` — undocumented
  or newly-arrived keys the docs do not mention
* the whole `usage` block, verbatim, token counts included
* `page_age` formats and the null-versus-absent split, and that a result
  carries no snippet field of any kind

WHAT IS SCRUBBED, because none of it is load-bearing for a wire contract:

* the subject's name, handle and personal domain, and the unrelated real people
  the search returned alongside them. They are swapped for the synthetic
  subject the rest of the suite already uses — Jane Smith of Acme Corp, GitHub
  `jsmith` — which also makes the fixture usable by the scoring tests.
* message and tool-use ids, which are request identifiers
* `encrypted_content`, replaced by a placeholder. It is opaque by design,
  meaningful only replayed to the same API, and the real blobs in this capture
  run 272 to 1,932 bytes each — kilobytes of unreadable base64 in a tracked
  file, for nothing.

**Platform hosts are deliberately NOT scrubbed.** `x.com`, `linkedin.com`,
`youtube.com` and `en.wikipedia.org` are matched by name in `discovery.py` and
`scoring.py` — as a profile host, a refused platform, a profile host again —
so swapping them would throw away the categories that make the fixture worth
having. Only the person-identifying parts of those URLs change.

The second fixture, `web_search_response_with_citations.json`, is **not** a
capture and says so in its own name, exactly as `github_events_with_commits.json`
does. It exists because the citation path has no live example to run against:
`SEARCH_SYSTEM` tells the model to reply with the single word "done", a model
that writes nothing cites nothing, and all nine captured responses came back
with `citations: null`. The snippet code is therefore unreachable in production
today, and a fixture written to the documented citation shape is the only thing
keeping it honest. That is a real finding about the tool, not a gap in the
capture — see `corpus/search/contract.py`.

Run: python tests/fixtures/_scrub_search.py   (requires captures/)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
CAPTURES = HERE.parents[1] / "captures"
BASE_CAPTURE = "blog_or_essay_or_writing"

QUERY = '"Jane Smith" blog OR essay OR writing'
MESSAGE_ID = "msg_scrubbed_placeholder"
TOOL_USE_ID = "srvtoolu_scrubbed_placeholder"

# Every result in the capture, mapped by hand rather than by regex. Ten entries
# is few enough to read, and each one carries a *category* the scorer keys on —
# a table states which, where a substitution pass would silently drop it. The
# script fails if the capture holds a URL that is not in here, so a re-capture
# cannot quietly produce a half-scrubbed fixture.
RESULTS: dict[str, tuple[str, str, str]] = {
    # real url: (fake url, fake title, what this result is here to preserve)
    "http://dustintran.com/blog/tutorial-on-inference-networks-2": (
        "http://janesmith.example/blog/tutorial-on-inference-networks-2",
        "Inference networks | Jane Smith",
        "their own site, over plain http — search returns both schemes",
    ),
    "https://dustintran.com/blog/hello-world": (
        "https://janesmith.example/blog/hello-world",
        "Hello world! | Jane Smith",
        "their own site, https, no page_age",
    ),
    "https://www.princetonreview.com/academic-tutoring/tutor/dustin%20d--12088353": (
        "https://tutorfinder.example/academic-tutoring/tutor/jane%20s--12088353",
        "Jane S. | Tutor in Essay Writing",
        "a directory listing for a different person; note the percent-encoding",
    ),
    "https://www.youtube.com/watch?v=gH8WLvexzeM": (
        "https://www.youtube.com/watch?v=SCRUBBEDvideo",
        "Write a short essay on Jane | Essay Writing | English - YouTube",
        "a profile host, and a result about neither the subject nor their writing",
    ),
    "https://www.linkedin.com/in/dustinvtran": (
        "https://www.linkedin.com/in/jsmith",
        "Jane Smith - VP Engineering at Acme Corp",
        "a refused platform, returned first-page for the right person",
    ),
    "http://dustintran.com/blog/on-the-paradigms-of-ai": (
        "http://janesmith.example/blog/on-the-paradigms-of-ai",
        "On the paradigms of AI | Jane Smith",
        "their own site again, with an older page_age",
    ),
    "https://x.com/dustinvtran": (
        "https://x.com/jsmith",
        "Jane Smith (@jsmith) / Posts / X",
        "a profile host: an identity, never a source",
    ),
    "https://www.linkedin.com/in/dustintran-/": (
        "https://www.linkedin.com/in/janesmith-/",
        "Jane Smith - Data Engineer - Associate @ Angle Systems ...",
        "a DIFFERENT person with the same name, on the same platform",
    ),
    "https://dustintran.com/blog/a-research-to-engineering-workflow": (
        "https://janesmith.example/blog/a-research-to-engineering-workflow",
        "A Research to Engineering Workflow | Jane Smith",
        "their own site, the fourth of four",
    ),
    "https://en.wikipedia.org/wiki/Truong_Tran": (
        "https://en.wikipedia.org/wiki/Jamie_Smyth",
        "Jamie Smyth",
        "a different person the name search dragged in",
    ),
}

# What a citation looks like, per the docs. Not observed — see the docstring.
CITED_TEXT = (
    "Rubrics beat interviews because they force you to decide what you are "
    "measuring before you meet anyone."
)


def scrub(raw: dict[str, Any]) -> dict[str, Any]:
    """One captured message, with identity swapped and blobs dropped."""
    message = json.loads(json.dumps(raw))
    message["id"] = MESSAGE_ID

    for block in message["content"]:
        if block["type"] == "server_tool_use":
            block["id"] = TOOL_USE_ID
            block["input"]["query"] = QUERY
        elif block["type"] == "web_search_tool_result":
            block["tool_use_id"] = TOOL_USE_ID
            for index, result in enumerate(block["content"]):
                url = result["url"]
                if url not in RESULTS:
                    raise SystemExit(
                        f"{url} is not in the scrub table. The capture changed; add it "
                        f"with the category it preserves rather than letting a real "
                        f"person's URL into a tracked fixture."
                    )
                fake_url, fake_title, _why = RESULTS[url]
                result["url"] = fake_url
                result["title"] = fake_title
                result["encrypted_content"] = f"OPAQUE-BLOB-PLACEHOLDER-{index + 1}"
    return message


def with_citations(scrubbed: dict[str, Any]) -> dict[str, Any]:
    """The documented citation shape, bolted onto the real envelope.

    SYNTHETIC. The live API returned `citations: null` on every one of nine
    responses, so this is the documented shape and nothing more — the same
    status, and for the same reason, as `github_events_with_commits.json`.
    """
    message = json.loads(json.dumps(scrubbed))
    first = message["content"][1]["content"][0]
    for block in message["content"]:
        if block["type"] == "text":
            block["text"] = "done"
            block["citations"] = [
                {
                    "type": "web_search_result_location",
                    "url": first["url"],
                    "title": first["title"],
                    "encrypted_index": "OPAQUE-INDEX-PLACEHOLDER",
                    "cited_text": CITED_TEXT,
                }
            ]
    return message


def provenance(real: bool) -> str:
    if real:
        return (
            "REAL. Captured from the wire on 2026-08-03 with `--capture-search`, one of "
            "nine responses from a live run, then scrubbed by tests/fixtures/_scrub_search.py "
            "— every key name, nesting, null and token count is exactly what arrived. "
            "Identity (names, handles, personal domain, ids) is swapped for the suite's "
            "synthetic subject and encrypted_content is a placeholder; platform hosts are "
            "kept because the scorer matches them by name. The load-bearing finding is "
            "`citations: null`: a web_search_result has no snippet field, snippets come "
            "from model citations, and there were none in any of the nine."
        )
    return (
        "SYNTHETIC, and deliberately so — the citation path has no live example. All nine "
        "captured responses carried `citations: null`, because SEARCH_SYSTEM tells the "
        "model to reply with one word and a model that writes nothing cites nothing. This "
        "is the documented citation shape on the real envelope, so the snippet code has "
        "something to run against. Same status as github_events_with_commits.json."
    )


def main() -> None:
    matches = sorted(CAPTURES.glob(f"*{BASE_CAPTURE}*.json"))
    if not matches:
        raise SystemExit(f"no capture matching *{BASE_CAPTURE}*.json in {CAPTURES}")
    raw = json.loads(matches[0].read_text(encoding="utf-8"))["raw_message"]

    real = scrub(raw)
    real = {"_provenance": provenance(True), **real}
    (HERE / "web_search_response.json").write_text(
        json.dumps(real, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    synthetic = with_citations({k: v for k, v in real.items() if k != "_provenance"})
    synthetic = {"_provenance": provenance(False), **synthetic}
    (HERE / "web_search_response_with_citations.json").write_text(
        json.dumps(synthetic, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("wrote 2 search fixtures")


if __name__ == "__main__":
    main()
