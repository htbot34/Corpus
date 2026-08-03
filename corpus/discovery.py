"""Phase 1 discovery: follow links out from the anchors.

The cheapest and most reliable half of finding someone's writing, and the one
worth exhausting before paying for search: people link their own things. A
GitHub bio pointing at a blog is a self-declaration, not a guess, so the blog
inherits near-certain attribution and the search phase never has to consider
it.

Nothing here costs money. Every fetch is plain HTTP against a public page, and
every response is cached, so a second run over the same target is free.

What makes a link `linked` rather than a lead
---------------------------------------------
"Reached by following a link from an anchor" is not sufficient on its own: a
personal site links to other people's blogs constantly, and treating those as
the subject's would be exactly the failure this whole layer exists to prevent.
So surfaces are split in two:

* **Declared** surfaces are fields a person filled in about themselves — the
  GitHub profile `blog` field, the X bio URL, the links in an X bio. A link
  there *is* a claim of ownership, so it is `linked` unconditionally.
* **Page** surfaces are bodies of text — a profile README, a Substack about
  page, a site's homepage. A link there is `linked` only with a corroborating
  signal: it sits on a host we already know is theirs, or the host carries
  their name. Otherwise it is held as `name_match` for a human to confirm.

That distinction is the whole scoring model at this phase. Search adds a third
kind of evidence in Phase 2; it does not change this one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from .cache import Cache
from .identity import IdentityCard
from .models import ATTRIBUTION_CONFIDENCE, Attribution
from .sources.base import SourceError, html_to_text, http_client

# A cap on plain-HTTP fetches, not on money — there is none to spend here. It
# exists so a site with a hostile link graph cannot turn a free phase into a
# thousand requests against someone else's server.
DEFAULT_MAX_FETCHES = 25

# Probed in this order, and only when the site advertised no feed of its own.
# Straight from the shapes people's blogs actually take; a site that declares
# `<link rel="alternate" type="application/rss+xml">` costs zero extra fetches
# and skips all of this.
FEED_PROBES = ("/feed", "/rss.xml", "/atom.xml")
SECTION_PROBES = ("/blog", "/writing", "/essays")

# Hosts that are never the subject's writing: build badges, licences, CDNs,
# package registries, payment links. Suffix-matched, so a subdomain counts.
INFRA_HOSTS = (
    "shields.io",
    "badge.fury.io",
    "travis-ci.org",
    "travis-ci.com",
    "codecov.io",
    "coveralls.io",
    "circleci.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "gravatar.com",
    "w3.org",
    "schema.org",
    "creativecommons.org",
    "gnu.org",
    "opensource.org",
    "apache.org",
    "mit-license.org",
    "wordpress.org",
    "gohugo.io",
    "jekyllrb.com",
    "netlify.com",
    "vercel.com",
    "npmjs.com",
    "pypi.org",
    "crates.io",
    "hub.docker.com",
    "stackoverflow.com",
    "wikipedia.org",
    "paypal.com",
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "t.co",
    "bit.ly",
    "goo.gl",
)

# Hosts where a URL identifies a person rather than carrying their writing.
# A match here corroborates the card — "the site links back to the GitHub
# anchor" is real evidence — but it is not a document source.
PROFILE_HOSTS = (
    "x.com",
    "twitter.com",
    "github.com",
    "gitlab.com",
    "bsky.app",
    "threads.net",
    "mastodon.social",
    "news.ycombinator.com",
    "orcid.org",
    "scholar.google.com",
    "keybase.io",
    "goodreads.com",
    "youtube.com",
    "youtu.be",
)

# Platforms this tool will not read, restated at the link level so a link to
# one is refused with the reason rather than quietly dropped.
REFUSED_HOSTS = {
    "linkedin.com": "LinkedIn blocks automated access and forbids it in their terms",
    "facebook.com": "a personal Facebook profile is not public data",
    "instagram.com": "Instagram requires an authenticated session",
}

# Blogging platforms: the host is shared, so the *path or subdomain* is the
# identity. Content, not profile.
BLOG_PLATFORMS = (
    "substack.com",
    "medium.com",
    "ghost.io",
    "bearblog.dev",
    "write.as",
    "hashnode.dev",
    "dev.to",
    "wordpress.com",
    "blogspot.com",
    "svbtle.com",
    "micro.blog",
)

_FEED_LINK = re.compile(
    r"<link[^>]+rel=[\"']?alternate[\"']?[^>]*>",
    re.I,
)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
_FEED_TYPE = re.compile(r"type=[\"']?application/(?:rss|atom)\+xml", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _suffix_match(host: str, needles: Iterable[str]) -> str:
    for needle in needles:
        if host == needle or host.endswith("." + needle):
            return needle
    return ""


def normalize_url(url: str) -> str:
    """One canonical spelling per page, so the same find is not counted twice."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().removeprefix("www.")
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    # A site root is spelled without the trailing slash, so it matches the
    # anchor form the user typed and dedupes against it.
    path = parsed.path.rstrip("/")
    # Fragments and tracking parameters name a scroll position and a referrer,
    # not a document.
    query = "&".join(
        p
        for p in parsed.query.split("&")
        if p and not p.lower().startswith(("utm_", "ref=", "source=", "fbclid", "gclid"))
    )
    return urlunparse((parsed.scheme, host, path, "", query, ""))


def _is_site_root(url: str) -> bool:
    return urlparse(url).path.rstrip("/") in ("", "/")


def _looks_like_feed(body: str) -> bool:
    head = body.lstrip()[:400].lower()
    return "<rss" in head or "<feed" in head or ("<?xml" in head and "<channel" in body[:2000])


def _kind_for(url: str) -> str:
    """Which adapter should read this URL."""
    host = _host(url)
    path = urlparse(url).path.lower()
    if host.endswith(".substack.com") or _suffix_match(host, ("substack.com",)):
        return "substack"
    if path.endswith((".xml", ".rss", ".atom")) or path.rstrip("/").endswith(
        ("/feed", "/rss", "/atom")
    ):
        return "rss"
    return "web"


# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """One place the subject's writing might live, and why we think so."""

    url: str
    kind: str  # "rss" | "substack" | "web" | "github" | "x"
    attribution: Attribution
    basis: str
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.confidence:
            self.confidence = ATTRIBUTION_CONFIDENCE[self.attribution]

    @property
    def ingestible(self) -> bool:
        """Is there an adapter for this kind yet?

        `github` is discovered before `sources/github.py` exists. Saying so is
        better than dropping the find: the URL is in discovery.json either way,
        and the run reports what it could not read.
        """
        return self.kind in ("rss", "substack", "web")

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "attribution": self.attribution,
            "attribution_basis": self.basis,
            "attribution_confidence": round(self.confidence, 3),
            "signals": list(self.signals),
            "missing": list(self.missing),
        }


@dataclass
class DiscoveryResult:
    """What Phase 1 found, what it held back, and what it could not read."""

    #: Ingested by default: anchor, linked, corroborated.
    candidates: list[Candidate] = field(default_factory=list)
    #: name_match — a human decides. Never ingested without being accepted.
    held: list[Candidate] = field(default_factory=list)
    #: Corroborating facts that are not sources: "the site links back to GitHub".
    identity_signals: list[str] = field(default_factory=list)
    fetches: int = 0
    cached_fetches: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_attribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in self.candidates + self.held:
            counts[candidate.attribution] = counts.get(candidate.attribution, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.as_dict() for c in self.candidates],
            "held": [c.as_dict() for c in self.held],
            "identity_signals": list(self.identity_signals),
            "fetches": self.fetches,
            "cached_fetches": self.cached_fetches,
            "by_attribution": self.by_attribution(),
            "notes": list(self.notes),
            "errors": list(self.errors),
            "searches": 0,  # Phase 2 sets this; stated so its absence is explicit
        }


@dataclass
class _Surface:
    """One place links were read from, and whether being there is a claim."""

    label: str
    declared: bool
    links: list[str]


# --------------------------------------------------------------------------


class _Fetcher:
    """Cached GETs with a hard ceiling and no exceptions escaping.

    Discovery runs before anything has been paid for and must never be the
    reason a run dies: a site that is down, slow, or hostile degrades the
    corpus, it does not end the run.
    """

    def __init__(self, cache: Cache, max_fetches: int, log: Callable[[str], None]) -> None:
        self.cache = cache
        self.max_fetches = max_fetches
        self.log = log
        self.fetches = 0
        self.cached = 0
        self.errors: list[str] = []
        self._client: Any = None

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> str | None:
        cached = self.cache.get("discovery", f"get:{url}")
        if cached is not None:
            self.cached += 1
            return str(cached)
        if self.cache.offline:
            self.errors.append(f"--offline: {url} is not cached")
            return None
        if self.fetches >= self.max_fetches:
            self.errors.append(
                f"fetch cap of {self.max_fetches} reached before {url} "
                "(raise --max-fetches to look further)"
            )
            return None
        self.fetches += 1
        try:
            if self._client is None:
                self._client = http_client()
            resp = self._client.get(url, headers=headers or {})
            if resp.status_code >= 400:
                self.errors.append(f"{resp.status_code} fetching {url}")
                return None
            body = str(resp.text)
        except Exception as exc:  # network, TLS, redirect loops — all non-fatal
            self.errors.append(str(SourceError(f"fetching {url}: {exc}")))
            return None
        self.cache.put("discovery", f"get:{url}", body)
        return body

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _github_headers() -> dict[str, str]:
    """Unauthenticated is 60 requests/hour; a token raises it to 5,000.

    Optional on purpose — discovery reads at most two public endpoints per
    target, which fits comfortably in the anonymous allowance.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# --------------------------------------------------------------------------
# surfaces
# --------------------------------------------------------------------------


def _links_in_text(text: str) -> list[str]:
    return _URL_IN_TEXT.findall(text or "")


def _x_surfaces(profile: dict[str, Any]) -> list[_Surface]:
    """The X bio: a declared surface, and free — the profile is already paid for."""
    links: list[str] = []
    for url in (profile.get("url"), profile.get("expandedUrl")):
        if url:
            links.append(str(url))
    entities = profile.get("entities") or {}
    for section in ("url", "description"):
        for entry in (entities.get(section) or {}).get("urls") or []:
            expanded = entry.get("expanded_url") or entry.get("url")
            if expanded:
                links.append(str(expanded))
    links.extend(_links_in_text(str(profile.get("description") or "")))
    handle = profile.get("userName") or profile.get("screen_name") or "x"
    return [_Surface(f"@{handle}'s X bio", declared=True, links=links)]


def _pinned_surfaces(
    profile: dict[str, Any],
    lookup: Callable[[list[str]], dict[str, dict[str, Any]]],
    errors: list[str],
) -> list[_Surface]:
    """Links out of the pinned posts.

    A pinned post is the one thing a person chose to keep at the top of their
    profile, which makes a link in it about as deliberate as a bio link. This
    is the only part of discovery that touches a metered API — one batch
    lookup, the same order of cost as the profile read that precedes it — so it
    is skipped entirely under --dry-run.
    """
    ids = [str(i) for i in (profile.get("pinnedTweetIds") or []) if i]
    if not ids:
        return []
    try:
        tweets = lookup(ids[:3])
    except Exception as exc:
        errors.append(f"pinned post lookup failed (non-fatal): {exc}")
        return []
    links: list[str] = []
    for raw in tweets.values():
        entities = (raw.get("entities") or {}).get("urls") or []
        for entry in entities:
            expanded = entry.get("expanded_url") or entry.get("url")
            if expanded:
                links.append(str(expanded))
        links.extend(_links_in_text(str(raw.get("text") or raw.get("full_text") or "")))
    return [_Surface("their pinned post", declared=True, links=links)]


def _github_surfaces(login: str, fetcher: _Fetcher) -> tuple[list[_Surface], list[str]]:
    """The GitHub profile: the `blog` field is declared, the README is a page.

    A profile README is written by its owner, but it is prose — full of links
    to other people's projects, badges, and papers. Treating it as declared
    would attribute half of someone's reading list to them.
    """
    surfaces: list[_Surface] = []
    signals: list[str] = []
    raw = fetcher.get(f"https://api.github.com/users/{login}", headers=_github_headers())
    if raw:
        try:
            profile = json.loads(raw)
        except json.JSONDecodeError:
            profile = {}
        declared: list[str] = []
        blog = str(profile.get("blog") or "").strip()
        if blog:
            declared.append(blog if "//" in blog else f"https://{blog}")
        declared.extend(_links_in_text(str(profile.get("bio") or "")))
        if declared:
            surfaces.append(_Surface(f"github.com/{login}'s bio", declared=True, links=declared))
        if profile.get("twitter_username"):
            signals.append(f"github.com/{login} names @{profile['twitter_username']} on X")
        for label in ("company", "location", "name"):
            if profile.get(label):
                signals.append(f"github.com/{login} {label}: {profile[label]}")

    readme = fetcher.get(f"https://raw.githubusercontent.com/{login}/{login}/HEAD/README.md")
    if readme:
        surfaces.append(
            _Surface(
                f"github.com/{login}'s profile README",
                declared=False,
                links=_links_in_text(readme),
            )
        )
    return surfaces, signals


def _page_surface(url: str, label: str, fetcher: _Fetcher) -> _Surface | None:
    body = fetcher.get(url)
    if body is None:
        return None
    _, links = html_to_text(body)
    links.extend(feed_links(body, url))
    return _Surface(label, declared=False, links=links)


def feed_links(html: str, base_url: str) -> list[str]:
    """Feeds the page declares in its own `<head>`.

    Worth doing before probing: a site that advertises its feed costs zero
    extra requests, and the probe list below only runs when it does not.
    """
    found: list[str] = []
    for tag in _FEED_LINK.findall(html or ""):
        if not _FEED_TYPE.search(tag):
            continue
        href = _HREF.search(tag)
        if href:
            found.append(urljoin(base_url, href.group(1)))
    return found


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _path_carries(url: str, keys: set[str]) -> str:
    """Does the host or the first path segment carry one of their names?"""
    host = _host(url)
    labels = host.split(".")
    registrable = labels[-2] if len(labels) > 1 else host
    segments = [s for s in urlparse(url).path.split("/") if s]
    haystacks = {registrable.replace("-", "")}
    haystacks.update(s.lower().replace("-", "").replace("_", "") for s in segments[:1])
    haystacks.update(label.replace("-", "") for label in labels[:-2])
    for key in keys:
        for hay in haystacks:
            if key and key in hay:
                return key
    return ""


def score_link(
    url: str,
    surface: _Surface,
    card: IdentityCard,
    anchor_hosts: set[str],
) -> tuple[Candidate | None, str]:
    """Turn one outbound link into a candidate, a signal, or nothing.

    Returns (candidate, note). Exactly one of the two is ever interesting; a
    note without a candidate is the reason it was dropped or the corroborating
    fact it turned into.
    """
    clean = normalize_url(url)
    if not clean:
        return None, ""
    if card.is_excluded(clean):
        return None, f"{clean}: excluded by the card"

    host = _host(clean)
    if not host:
        return None, ""
    if _suffix_match(host, INFRA_HOSTS):
        return None, ""

    refused = _suffix_match(host, REFUSED_HOSTS)
    if refused:
        return None, f"{clean}: not read — {REFUSED_HOSTS[refused]}"

    is_platform = bool(_suffix_match(host, BLOG_PLATFORMS))
    if not is_platform and _suffix_match(host, PROFILE_HOSTS):
        # A profile page, not writing. It still corroborates the card when it
        # carries a handle we already trust.
        match = _path_carries(clean, card.name_keys())
        if match:
            return None, f"{clean}: profile page matching '{match}' — corroborates the card"
        return None, ""

    same_host = host in anchor_hosts or any(host.endswith("." + a) for a in anchor_hosts)
    name_match = _path_carries(clean, card.name_keys())
    signals: list[str] = []
    if same_host:
        signals.append("sits on a host the card already anchors")
    if surface.declared:
        signals.append(f"self-declared in {surface.label}")
    if name_match:
        signals.append(f"host or path carries '{name_match}'")

    kind = _kind_for(clean)
    if signals:
        # Within the tier: more agreeing signals is more confidence, but a
        # linked find is never promoted past linked by being very linked.
        confidence = min(0.95, ATTRIBUTION_CONFIDENCE["linked"] + 0.05 * (len(signals) - 1))
        return (
            Candidate(
                url=clean,
                kind=kind,
                attribution="linked",
                basis=f"linked from {surface.label}",
                confidence=confidence,
                signals=signals,
            ),
            "",
        )

    missing = ["not on an anchored host", "no name in the host or path"]
    if not surface.declared:
        missing.append(f"{surface.label} is prose, not a self-declaration")
    return (
        Candidate(
            url=clean,
            kind=kind,
            attribution="name_match",
            basis=f"appeared on {surface.label}, nothing else matched",
            signals=[f"appeared on {surface.label}"],
            missing=missing,
        ),
        "",
    )


# --------------------------------------------------------------------------
# the phase itself
# --------------------------------------------------------------------------


def plan_from_anchors(card: IdentityCard) -> list[str]:
    """What Phase 1 would fetch, without fetching any of it. For --dry-run."""
    plan: list[str] = []
    for kind, value in card.anchors.items():
        if kind == "x":
            plan.append(f"read @{value}'s X bio (already fetched with the profile)")
        elif kind == "github":
            plan.append(f"GET api.github.com/users/{value} and their profile README")
        elif kind == "site":
            plan.append(f"GET {value}, then its declared feed or up to 6 probe paths")
        elif kind == "substack":
            plan.append(f"GET https://{value}/about")
        elif kind == "rss":
            plan.append(f"read {value} directly (anchor)")
    return plan


# anchor kind -> (URL to read, which adapter reads it). The adapter is taken
# from the anchor kind rather than sniffed from the URL: the user already told
# us what this is, and guessing could only disagree with them.
_SEED_SHAPES: dict[str, tuple[str, str]] = {
    "github": ("https://github.com/{}", "github"),
    "site": ("{}", "web"),
    "substack": ("https://{}", "substack"),
    "rss": ("{}", "rss"),
}


def _seed_candidates(card: IdentityCard) -> list[Candidate]:
    """The anchors themselves. Certain by definition — the user supplied them."""
    seeds: list[Candidate] = []
    for kind, value in card.anchors.items():
        if kind == "x":
            continue  # ingested by the X pipeline, not as a discovered source
        template, adapter = _SEED_SHAPES[kind]
        seeds.append(
            Candidate(
                url=normalize_url(template.format(value)),
                kind=adapter,
                attribution="anchor",
                basis=f"{kind} anchor, supplied by the user",
                signals=["supplied as an anchor"],
            )
        )
    return seeds


def _probe_site(url: str, card: IdentityCard, fetcher: _Fetcher) -> list[Candidate]:
    """Resolve a site root to whatever it publishes.

    The declared feed is checked first and short-circuits everything: a site
    that advertises `<link rel="alternate">` costs one fetch — already a cache
    hit for an anchor site, whose homepage was read as a link surface — and
    only a site that does not gets the probe treatment.
    """
    found: list[Candidate] = []
    basis = f"probed from {_host(url)}"

    home = fetcher.get(url)
    if home:
        for feed in dict.fromkeys(normalize_url(f) for f in feed_links(home, url)):
            if feed and not card.is_excluded(feed):
                found.append(
                    Candidate(
                        url=feed,
                        kind="rss",
                        attribution="linked",
                        basis=f"feed declared in {_host(url)}'s <head>",
                        confidence=0.9,
                        signals=["the site advertises this feed itself"],
                    )
                )
        if found:
            return found

    for path in FEED_PROBES:
        candidate_url = normalize_url(urljoin(url + "/", path.lstrip("/")))
        body = fetcher.get(candidate_url)
        if body and _looks_like_feed(body):
            found.append(
                Candidate(
                    url=candidate_url,
                    kind="rss",
                    attribution="linked",
                    basis=f"{basis} — {path} is a feed",
                    confidence=0.9,
                    signals=["found by probing an anchored host"],
                )
            )
            return found

    for path in SECTION_PROBES:
        candidate_url = normalize_url(urljoin(url + "/", path.lstrip("/")))
        body = fetcher.get(candidate_url)
        if body is None:
            continue
        for feed in dict.fromkeys(normalize_url(f) for f in feed_links(body, candidate_url)):
            if feed and not card.is_excluded(feed):
                found.append(
                    Candidate(
                        url=feed,
                        kind="rss",
                        attribution="linked",
                        basis=f"feed declared on {path}",
                        confidence=0.9,
                        signals=["the site advertises this feed itself"],
                    )
                )
        if found:
            return found
        text, _ = html_to_text(body)
        if len(text) > 400:
            found.append(
                Candidate(
                    url=candidate_url,
                    kind="web",
                    attribution="linked",
                    basis=f"{basis} — {path} exists and has prose",
                    confidence=0.85,
                    signals=["found by probing an anchored host"],
                )
            )
            return found
    return found


def _better(a: Candidate, b: Candidate) -> Candidate:
    """Keep the stronger claim when the same URL is found twice."""
    from .models import ATTRIBUTION_ORDER

    rank = {name: i for i, name in enumerate(ATTRIBUTION_ORDER)}
    if rank[a.attribution] != rank[b.attribution]:
        return a if rank[a.attribution] < rank[b.attribution] else b
    winner, loser = (a, b) if a.confidence >= b.confidence else (b, a)
    for signal in loser.signals:
        if signal not in winner.signals:
            winner.signals.append(signal)
    return winner


def discover_from_anchors(
    card: IdentityCard,
    cache: Cache,
    *,
    x_profile: dict[str, Any] | None = None,
    x_lookup: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    follow_links: bool = True,
    max_fetches: int = DEFAULT_MAX_FETCHES,
    log: Callable[[str], None] = print,
) -> DiscoveryResult:
    """Follow every anchor out one hop. No search, no spend.

    `x_profile` is the profile the run already fetched — reusing it means the X
    bio costs nothing. `x_lookup` enables the pinned-post read, the one metered
    call in this phase; omit it and the phase is free outright.

    `follow_links=False` (`--no-discover`) returns the anchors and nothing else,
    without a single fetch. The anchors are still candidates: they are what the
    user asked for, and refusing to read them because crawling is switched off
    would confuse two different decisions.
    """
    result = DiscoveryResult()
    fetcher = _Fetcher(cache, max_fetches if follow_links else 0, log)
    anchor_hosts = card.anchor_hosts()

    try:
        surfaces: list[_Surface] = []
        if x_profile and follow_links:
            surfaces.extend(_x_surfaces(x_profile))
            if x_lookup is not None:
                surfaces.extend(_pinned_surfaces(x_profile, x_lookup, result.errors))
        if "github" in card.anchors and follow_links:
            github_surfaces, signals = _github_surfaces(card.anchors["github"], fetcher)
            surfaces.extend(github_surfaces)
            result.identity_signals.extend(signals)
        if "substack" in card.anchors and follow_links:
            about = _page_surface(
                f"https://{card.anchors['substack']}/about",
                f"{card.anchors['substack']}'s about page",
                fetcher,
            )
            if about is not None:
                surfaces.append(about)
        if "site" in card.anchors and follow_links:
            # One fetch, two uses: the homepage's outbound links are a surface,
            # and the same cached body answers the feed question below.
            home = _page_surface(
                card.anchors["site"], f"{_host(card.anchors['site'])}'s homepage", fetcher
            )
            if home is not None:
                surfaces.append(home)

        by_url: dict[str, Candidate] = {}

        def offer(candidate: Candidate) -> None:
            existing = by_url.get(candidate.url)
            by_url[candidate.url] = _better(existing, candidate) if existing else candidate

        for seed in _seed_candidates(card):
            offer(seed)

        for surface in surfaces:
            for link in surface.links:
                candidate, note = score_link(link, surface, card, anchor_hosts)
                if note:
                    result.notes.append(note)
                if candidate is not None:
                    offer(candidate)

        # A site root is an address, not a document. Probe the ones we trust
        # for what they actually publish — but only where nothing on that host
        # has turned up already, since a declared feed is both cheaper and
        # better evidence than a guessed path.
        def host_has_content(host: str) -> bool:
            return any(
                _host(c.url) == host and c.kind in ("rss", "substack")
                for c in by_url.values()
                if c.attribution in ("anchor", "linked")
            )

        for candidate in list(by_url.values()):
            if candidate.kind != "web" or candidate.attribution not in ("anchor", "linked"):
                continue
            if not _is_site_root(candidate.url) or host_has_content(_host(candidate.url)):
                continue
            for found in _probe_site(candidate.url, card, fetcher):
                offer(found)

        # A homepage is only worth reading as a document when its host yielded
        # nothing better. Otherwise it is a nav bar with a photograph.
        for url, candidate in list(by_url.items()):
            if candidate.kind == "web" and _is_site_root(url) and host_has_content(_host(url)):
                by_url.pop(url, None)

        for candidate in by_url.values():
            if candidate.attribution == "name_match":
                result.held.append(candidate)
            else:
                result.candidates.append(candidate)
    finally:
        fetcher.close()

    result.candidates.sort(key=lambda c: (-c.confidence, c.url))
    result.held.sort(key=lambda c: c.url)
    result.fetches = fetcher.fetches
    result.cached_fetches = fetcher.cached
    # Deduped: one dead host is reached from several directions, and repeating
    # the same failure five times buries the four other things that went wrong.
    result.errors.extend(e for e in dict.fromkeys(fetcher.errors) if e not in result.errors)
    result.notes = list(dict.fromkeys(result.notes))
    return result
