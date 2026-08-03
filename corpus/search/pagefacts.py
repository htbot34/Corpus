"""What a page says about who wrote it.

Structured author metadata is the single most valuable thing on a candidate
page, and it is the thing a search snippet can never contain. `<meta
name="author">`, JSON-LD `author`, OpenGraph `article:author`, and a feed's
`<author>` are all *declarations of authorship* by the publisher. A name
appearing in the body is not — it is as likely to be a citation of someone
else's work, and on a page about a person it is certain to be.

So the two are kept apart all the way through: `authors` holds declarations,
`bylines` holds "By Jane Smith" found in prose, and `text` holds everything
else. `scoring.py` weights them differently and must be able to tell them
apart.

Everything here is regex and stdlib. The repo already decided against a
parsing dependency for three optional adapters (`sources/base.py`), and this is
a smaller job than that one: find four specific attributes, not understand a
document.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from ..sources.base import html_to_text

# <meta name="author" content="Jane Smith"> in either attribute order, and the
# property= spelling OpenGraph uses.
_META = re.compile(
    r"<meta\b[^>]*?(?:name|property)\s*=\s*[\"']([^\"']+)[\"'][^>]*?"
    r"content\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
    re.I,
)
_META_REVERSED = re.compile(
    r"<meta\b[^>]*?content\s*=\s*[\"']([^\"']*)[\"'][^>]*?"
    r"(?:name|property)\s*=\s*[\"']([^\"']+)[\"'][^>]*>",
    re.I,
)
_JSON_LD = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_REL_AUTHOR = re.compile(
    r"<a\b[^>]*rel\s*=\s*[\"'][^\"']*\bauthor\b[^\"']*[\"'][^>]*>(.*?)</a>", re.I | re.S
)
_TAGS = re.compile(r"<[^>]+>")

# Feed author elements. Atom nests a <name> inside <author>; RSS puts an email
# or a name directly in <author>, and most real feeds use dc:creator instead.
_FEED_AUTHOR = re.compile(
    r"<(?:author|dc:creator|itunes:author)[^>]*>(.*?)</(?:author|dc:creator|itunes:author)>",
    re.I | re.S,
)
_FEED_NAME = re.compile(r"<name[^>]*>(.*?)</name>", re.I | re.S)

# Metadata keys that declare authorship, as opposed to merely mentioning a
# person. `twitter:creator` is a handle rather than a name and is read
# separately.
_AUTHOR_KEYS = {
    "author",
    "article:author",
    "og:article:author",
    "citation_author",
    "dc.creator",
    "dcterms.creator",
    "sailthru.author",
    "parsely-author",
    "byl",
}
_HANDLE_KEYS = {"twitter:creator", "twitter:site"}

# "By Jane Smith" as it appears in prose. Deliberately narrow: it must be at a
# line start or after a bullet, and the name must be capitalised words. A loose
# pattern here would turn "written by hand" into an author named "Hand".
_BYLINE = re.compile(
    r"(?:^|\n)\s*(?:by|written by|words by|author)\s*[:\-]?\s+"
    r"([A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){0,3})",
    re.M,
)

_NON_WORD = re.compile(r"[^a-z0-9\s]+")


def _strip_tags(value: str) -> str:
    return " ".join(_TAGS.sub(" ", value or "").split())


def normalize_name(value: str) -> list[str]:
    """A name as comparable tokens: lowercase, punctuation-free, no initials.

    Middle initials are dropped rather than matched, so "Jane A. Smith" and
    "Jane Smith" compare equal — they are the same person often enough that
    treating them as different people would hold back the right pages.
    """
    text = _NON_WORD.sub(" ", (value or "").lower())
    return [t for t in text.split() if len(t) > 1]


def name_matches(candidate: str, target: str) -> bool:
    """Is `candidate` plausibly the same name as `target`?

    First and last token must both be present, in order. That accepts
    "Jane A. Smith" and "Smith, Jane" while rejecting "Jane Doe" and the
    surname on its own — a bare surname is far too common to be evidence.
    """
    want = normalize_name(target)
    got = normalize_name(candidate)
    if len(want) < 2 or not got:
        return False
    first, last = want[0], want[-1]
    return first in got and last in got


@dataclass
class PageFacts:
    """Everything a fetched page says about its own authorship."""

    url: str = ""
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    #: The subset of `links` that sit in the page's own furniture — an author
    #: or byline block, the header, the footer — rather than in body prose.
    #: Where a page declares itself is worth more than what its prose mentions.
    declared_links: list[str] = field(default_factory=list)
    #: Declared authorship: meta tags, JSON-LD, feed <author>. Strong.
    authors: list[str] = field(default_factory=list)
    #: "By Jane Smith" in prose. Moderate — real, but forgeable by a quote.
    bylines: list[str] = field(default_factory=list)
    #: Handles the page declares (twitter:creator and friends), without the @.
    handles: list[str] = field(default_factory=list)
    #: True when the body was read; False for a snippet-only candidate.
    fetched: bool = False

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower().removeprefix("www.")

    @property
    def haystack(self) -> str:
        """Everything readable, lowercased, for substring checks."""
        return f"{self.title}\n{self.text}".lower()

    def declares_author(self, name: str) -> bool:
        return any(name_matches(a, name) for a in self.authors)

    def declares_other_author(self, name: str) -> bool:
        """Does the page name an author, and is none of them the target?

        The load-bearing half of "about them, not by them": a profile piece
        carries a real byline, and it belongs to the journalist.
        """
        return bool(self.authors) and not self.declares_author(name)

    def has_byline(self, name: str) -> bool:
        return any(name_matches(b, name) for b in self.bylines)


def _meta_pairs(html: str) -> list[tuple[str, str]]:
    pairs = [(k.strip().lower(), v.strip()) for k, v in _META.findall(html)]
    pairs += [(k.strip().lower(), v.strip()) for v, k in _META_REVERSED.findall(html)]
    return pairs


def _json_ld_authors(html: str) -> list[str]:
    """Author names from JSON-LD, however deeply nested.

    Schema.org allows `author` to be a string, an object with `name`, or a list
    of either, and publishers use all three. Walking the whole document rather
    than reaching for a fixed path is the only version that works on more than
    one CMS.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in ("author", "creator"):
                    found.extend(_names_in(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    def _names_in(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()]
        if isinstance(value, dict):
            name = value.get("name")
            return [str(name).strip()] if isinstance(name, str) else []
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                out.extend(_names_in(item))
            return out
        return []

    for block in _JSON_LD.findall(html):
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            # Malformed JSON-LD is extremely common and is not a page we should
            # refuse to read. Skip the block, keep the page.
            continue
    return found


# Elements whose links are the page's own furniture rather than its prose:
# <header>, <footer> and <address> by name, anything else by an author or
# byline word among its class/id/itemprop tokens. `rel="author"` marks the
# link itself. Tokens rather than substrings, because "authorization-notice"
# and "authoritative-guide" are prose wrappers, not bylines — while
# "post-author", "entry-byline" and "byline__name" still match on their own
# tokens, which is how real themes spell them.
_FURNITURE_TAGS = {"header", "footer", "address"}
_FURNITURE_WORDS = {"author", "byline"}
# The document element and <body> are never furniture, whatever their class
# says: WordPress stamps `author author-<slug>` onto <body> on author archive
# pages, themes add `single-author` sitewide, and a marker that swallows the
# whole page would turn every prose link into a declaration.
_NEVER_FURNITURE = {"html", "body"}
_ATTR_TOKENS = re.compile(r"[a-z0-9]+")

#: A class-marked author/byline block whose text runs past this is a wrapper
#: wearing the class, not a byline — real bylines and author bios run a line
#: or three. <header>/<footer> are exempt; site footers are legitimately big.
_FURNITURE_TEXT_CAP = 400

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class _Frame:
    """One open element, and the furniture links waiting on its close."""

    tag: str
    furniture: str  # "" | "tag" | "class"
    pending: list[str] = field(default_factory=list)
    text_len: int = 0


class _DeclaredLinkFinder(HTMLParser):
    """Which links sit in the page's own furniture?

    `html_to_text` collects every link on a page; this records the subset
    found inside a header, footer, or author/byline block, because
    `scoring.py` weighs a link the page carries while identifying itself
    differently from one its prose happens to mention.

    Promotion can rest on this, so every rule errs toward calling a link
    prose — which only ever means a human sees it:

    * A furniture element must actually close before its links count. An
      unclosed `<div class="author">` would otherwise swallow every link on
      the rest of the page.
    * A class-marked block that closes with more than `_FURNITURE_TEXT_CAP`
      characters of text inside it is a wrapper wearing the class, and its
      links are dropped.
    * `<body>` and `<html>` are never furniture.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._open: list[_Frame] = []

    @staticmethod
    def _marker(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if tag in _NEVER_FURNITURE:
            return ""
        if tag in _FURNITURE_TAGS:
            return "tag"
        blob = " ".join(
            value or "" for key, value in attrs if key in ("class", "id", "itemprop")
        ).lower()
        if _FURNITURE_WORDS & set(_ATTR_TOKENS.findall(blob)):
            return "class"
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = next((v for k, v in attrs if k == "href" and v), "")
            if not href.startswith("http"):
                return
            rel = next((v for k, v in attrs if k == "rel" and v), "").lower()
            if "author" in _ATTR_TOKENS.findall(rel) or self._marker(tag, attrs) == "class":
                # The link is its own declaration: `rel="author"`, or an
                # author/byline class on the anchor itself.
                self.links.append(href)
                return
            frame = next((f for f in reversed(self._open) if f.furniture), None)
            if frame is not None:
                frame.pending.append(href)
            return
        if tag in _VOID_TAGS:
            return
        self._open.append(_Frame(tag=tag, furniture=self._marker(tag, attrs)))

    def handle_data(self, data: str) -> None:
        length = len(data.strip())
        if not length:
            return
        for frame in self._open:
            if frame.furniture == "class":
                frame.text_len += length

    def handle_endtag(self, tag: str) -> None:
        # Pop back to the matching open tag — real pages leave <p> and <li>
        # open constantly. Children closed implicitly this way DROP their
        # pending links: an element that never closed cleanly does not get to
        # claim what came after it.
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag != tag:
                continue
            frame = self._open[index]
            del self._open[index:]
            if frame.furniture == "tag" or (
                frame.furniture == "class" and frame.text_len <= _FURNITURE_TEXT_CAP
            ):
                self.links.extend(frame.pending)
            return


def declared_links(body: str) -> list[str]:
    """The furniture links of one HTML page, deduplicated, in page order."""
    finder = _DeclaredLinkFinder()
    try:
        finder.feed(body)
        finder.close()
    except Exception:  # malformed markup: same salvage rule as html_to_text
        pass
    return list(dict.fromkeys(finder.links))


def extract_facts(body: str, url: str) -> PageFacts:
    """Read one fetched page (or feed) for who it says wrote it."""
    facts = PageFacts(url=url, fetched=True)
    if not body:
        return facts

    looks_like_feed = body.lstrip()[:400].lower().startswith(("<?xml", "<rss", "<feed"))

    authors: list[str] = []
    handles: list[str] = []
    for key, value in _meta_pairs(body):
        if not value:
            continue
        if key in _AUTHOR_KEYS:
            authors.append(value)
        elif key in _HANDLE_KEYS:
            handles.append(value.lstrip("@").strip())

    authors.extend(_json_ld_authors(body))
    authors.extend(_strip_tags(a) for a in _REL_AUTHOR.findall(body))

    if looks_like_feed:
        for block in _FEED_AUTHOR.findall(body):
            nested = _FEED_NAME.search(block)
            authors.append(_strip_tags(nested.group(1) if nested else block))

    title_match = _TITLE.search(body)
    facts.title = _strip_tags(title_match.group(1)) if title_match else ""

    text, links = html_to_text(body)
    facts.text = text
    facts.links = links
    if not looks_like_feed:
        facts.declared_links = declared_links(body)
    # A byline sits at the top of an article. Searching the whole body would
    # find "by" in every sentence of a long page and pick up the surrounding
    # capitalised words as a name.
    facts.bylines = [b.strip() for b in _BYLINE.findall(text[:1500])]

    facts.authors = [a for a in dict.fromkeys(a.strip() for a in authors) if a]
    facts.handles = [h for h in dict.fromkeys(handles) if h]
    return facts


def facts_from_snippet(url: str, title: str, snippet: str) -> PageFacts:
    """A candidate we have not fetched yet.

    `fetched=False` is the whole point: `scoring.py` refuses to award the
    strong metadata signals to a page nobody has read, and this is how it
    knows. A snippet is a lead.
    """
    return PageFacts(url=url, title=title or "", text=snippet or "", fetched=False)
