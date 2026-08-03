"""Does this page belong to the target? Deterministic, offline, free.

Pure functions over `PageFacts` and an `IdentityCard`. No model call, no
network, no clock. That is not an optimization — it is what makes this file
auditable: the decision that determines whether a stranger's essay lands in
someone's corpus can be read, argued with, and tested without spending
anything.

The rule the whole file serves
------------------------------
**Absence of confirmation is not the same as presence of contradiction.** A
scorer built only from positive signals accepts the wrong person cheerfully:
it never finds a reason not to. So negatives are first-class here, they are
checked before the positives are counted, and any unresolved one blocks
promotion no matter how many positives agree.

"Unresolved" is doing real work in that sentence. A page that names a
*different* employer for this name is a contradiction — unless the page also
names the right one, in which case it is a page that mentions two companies,
which is not evidence of anything. Each negative below names what would
resolve it.

The tiers
---------
* Two or more independent strong-or-moderate signals, no negatives →
  `corroborated`. Ingested.
* One signal, or any unresolved negative → `name_match`. Held for a human.
* A page *about* them rather than *by* them → recorded as context, never a
  corpus document. A profile piece, an interview write-up, and a conference
  bio contain none of their reasoning.
* An exclude hit, an aggregator, or a citation of someone else's work →
  rejected outright.

`linked` is deliberately unreachable from here. `linked` means "reached from a
declared field on an anchor", and search is not that — no quantity of search
evidence turns into a self-declaration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..discovery import INFRA_HOSTS, PROFILE_HOSTS, REFUSED_HOSTS, suffix_match
from ..identity import IdentityCard
from ..models import ATTRIBUTION_CONFIDENCE, Attribution
from .pagefacts import PageFacts, normalize_name

STRONG = "strong"
MODERATE = "moderate"
WEAK = "weak"

#: Independent strong-or-moderate signals needed to reach `corroborated`.
CORROBORATION_THRESHOLD = 2

# Data brokers and people-search sites. Rejected on sight, and not only because
# their pages are worthless as writing: aggregating non-public personal data is
# outside what this tool does at all (README, "What is deliberately not built").
# The refusal belongs here as well as in the docs, because a search engine will
# happily return one.
PEOPLE_SEARCH_HOSTS = (
    "spokeo.com",
    "whitepages.com",
    "beenverified.com",
    "intelius.com",
    "radaris.com",
    "mylife.com",
    "peoplefinders.com",
    "truepeoplesearch.com",
    "fastpeoplesearch.com",
    "thatsthem.com",
    "zabasearch.com",
    "peekyou.com",
    "usphonebook.com",
    "anywho.com",
    "rocketreach.co",
    "zoominfo.com",
    "apollo.io",
    "lusha.com",
    "signalhire.com",
    "contactout.com",
    "seamless.ai",
    "leadiq.com",
    "hunter.io",
    "clearbit.com",
)

# Directories and aggregators: a database entry about a person, or someone
# else's collection of links. Real pages, sometimes accurate, never their
# writing.
DIRECTORY_HOSTS = (
    "crunchbase.com",
    "pitchbook.com",
    "owler.com",
    "f6s.com",
    "angel.co",
    "wellfound.com",
    "muckrack.com",
    "researchgate.net",
    "academia.edu",
    "about.me",
    "linktr.ee",
    "sessionize.com",
    "papercall.io",
    "news.google.com",
    "techmeme.com",
    "flipboard.com",
    "feedly.com",
    "paper.li",
    "scoop.it",
    "alltop.com",
)

# URL and title shapes that mean "this page is about a person". A page reached
# on their own anchored host is exempt — an /about page on their own site is
# written by them, and is one of the better documents in any corpus.
ABOUT_MARKERS = (
    "interview",
    "profile-of",
    "/profiles/",
    "q-and-a",
    "qanda",
    "speaker",
    "our-team",
    "/team/",
    "/staff/",
    "/people/",
    "biography",
    "obituary",
)

# "via @jane", "h/t Jane Smith" — the name is a credit for someone else's work,
# so the page is not a source of *their* writing however often it names them.
CITATION_CUES = (
    "via",
    "h/t",
    "hat tip",
    "hat-tip",
    "according to",
    "cited by",
    "cited in",
    "quoted in",
    "quoting",
    "credit to",
    "credit:",
    "source:",
    "referenced by",
    "as noted by",
    "reported by",
)

# Third-person speech attribution. Two or more of these about the target, with
# no declaration that they wrote the page, is a piece *about* them.
_ABOUT_VERBS = (
    "said",
    "says",
    "told",
    "tells",
    "explained",
    "explains",
    "argued",
    "argues",
    "recalled",
    "recalls",
    "added",
    "noted",
    "describes",
    "described",
)

# Dropped before comparing two organisation names, so "Acme, Inc." and "Acme"
# are the same employer and a suffix mismatch is not a contradiction.
_ORG_NOISE = {
    "inc",
    "inc.",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "company",
    "co",
    "co.",
    "plc",
    "gmbh",
    "the",
    "group",
    "holdings",
    "labs",
    "technologies",
    "tech",
}

# Dropped before comparing two role descriptions. Seniority and connective
# words are shared by every job title, so leaving them in makes "Senior
# Engineer" and "Senior Designer" look like a match.
_ROLE_NOISE = {
    "senior",
    "staff",
    "principal",
    "lead",
    "head",
    "chief",
    "deputy",
    "associate",
    "assistant",
    "junior",
    "of",
    "the",
    "and",
    "at",
    "for",
    "in",
    "a",
    "an",
}

_ORG_AFTER_NAME = re.compile(
    r"\b(?:at|of|with|for|joined|works\s+at)\s+"
    r"((?:[A-Z][\w&.\-]*)(?:\s+(?:[A-Z][\w&.\-]*|of|the|and)){0,3})"
)
_BASED_IN = re.compile(
    r"\b(?:based\s+in|located\s+in|lives\s+in|writes\s+from|from)\s+"
    r"((?:[A-Z][\w.\-]*)(?:[,\s]+[A-Z][\w.\-]*){0,2})"
)
_ROLE_AFTER_NAME = re.compile(
    r"\b(?:is|was|,)\s+(?:an?\s+|the\s+)?"
    r"([a-z][\w\-]*(?:\s+[a-z][\w\-]*){0,3})\s+(?:at|for|with|of)\b"
)

# How far after a name to look for a claim about that person. Long enough for
# "Jane Smith, a staff engineer at Acme Corp", short enough that the next
# sentence about somebody else is out of range.
_CLAIM_WINDOW = 140
_CITATION_WINDOW = 48


@dataclass(frozen=True)
class Signal:
    """One reason to believe this is theirs."""

    name: str
    weight: str  # STRONG | MODERATE | WEAK
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Negative:
    """One reason not to.

    `effect` is "reject" for a disqualification, or STRONG/MODERATE for a
    contradiction that blocks promotion without disqualifying the page.
    `resolved_by` names what would have made it go away, so the report can say
    why a page was held rather than only that it was.
    """

    name: str
    effect: str
    detail: str
    resolved_by: str = ""

    def __str__(self) -> str:
        return self.detail


@dataclass
class CandidateScore:
    """The verdict on one candidate, and every reason behind it."""

    url: str
    #: "corroborated" | "held" | "context" | "rejected"
    outcome: str
    signals: list[Signal] = field(default_factory=list)
    negatives: list[Negative] = field(default_factory=list)
    #: Whether the target's name appears on the page at all. Not a signal —
    #: it is the baseline every candidate clears — but the common-name
    #: heuristic counts it.
    name_present: bool = False
    fetched: bool = False
    query: str = ""

    @property
    def attribution(self) -> Attribution:
        """Never better than corroborated. Search cannot reach `linked`."""
        return "corroborated" if self.outcome == "corroborated" else "name_match"

    @property
    def confidence(self) -> float:
        base = ATTRIBUTION_CONFIDENCE[self.attribution]
        if self.outcome != "corroborated":
            return base
        # More agreeing signals is more confidence within the tier, never
        # across it: a corroborated match is not promoted by being very
        # corroborated.
        extra = max(0, self.independent_count - CORROBORATION_THRESHOLD)
        return min(0.8, base + 0.05 * extra)

    @property
    def ingestible(self) -> bool:
        return self.outcome == "corroborated"

    @property
    def independent_count(self) -> int:
        return count_independent(self.signals)

    @property
    def matched(self) -> list[str]:
        return [s.detail for s in self.signals]

    @property
    def missing(self) -> list[str]:
        """What did not match, in the words unconfirmed.md prints."""
        out = [n.detail for n in self.negatives]
        if not self.fetched:
            out.append("the page was not fetched, so only the snippet was scored")
        if self.independent_count < CORROBORATION_THRESHOLD and not self.negatives:
            out.append(
                f"only {self.independent_count} independent signal(s); "
                f"{CORROBORATION_THRESHOLD} are needed without a human saying otherwise"
            )
        return out

    @property
    def basis(self) -> str:
        found = "; ".join(self.matched) or "nothing beyond the name"
        where = "verified against the fetched page" if self.fetched else "snippet only"
        return f"found by search ({where}) — {found}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "outcome": self.outcome,
            "attribution": self.attribution,
            "attribution_confidence": round(self.confidence, 3),
            "query": self.query,
            "fetched": self.fetched,
            "signals": [
                {"name": s.name, "weight": s.weight, "detail": s.detail} for s in self.signals
            ],
            "negatives": [
                {"name": n.name, "effect": n.effect, "detail": n.detail} for n in self.negatives
            ],
            "matched": self.matched,
            "missing": self.missing,
        }


# Signals that are two views of the same fact. `<meta name="author">` and a
# visible "By Jane Smith" usually come from one byline, so counting both would
# manufacture corroboration out of a single claim. The group counts once, at
# its strongest weight.
_SIGNAL_GROUPS: dict[str, str] = {
    "author_metadata": "authorship",
    "byline_name": "authorship",
}


def count_independent(signals: list[Signal]) -> int:
    """Strong-and-moderate signals, deduplicated by what they actually claim."""
    seen: set[str] = set()
    for signal in signals:
        if signal.weight == WEAK:
            continue
        seen.add(_SIGNAL_GROUPS.get(signal.name, signal.name))
    return len(seen)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _normalize_org(value: str) -> set[str]:
    tokens = re.sub(r"[^a-z0-9\s]+", " ", (value or "").lower()).split()
    return {t for t in tokens if t not in _ORG_NOISE and len(t) > 1}


def _normalize_role(value: str) -> set[str]:
    tokens = re.sub(r"[^a-z0-9\s]+", " ", (value or "").lower()).split()
    return {t for t in tokens if t not in _ROLE_NOISE and len(t) > 1}


def name_pattern(name: str) -> re.Pattern[str] | None:
    """Match the target's name in prose, tolerating a middle initial."""
    tokens = normalize_name(name)
    if len(tokens) < 2:
        return None
    first, last = re.escape(tokens[0]), re.escape(tokens[-1])
    return re.compile(rf"\b{first}\b(?:\W+\w\.?)?\W+\b{last}\b", re.I)


def _anchor_urls(card: IdentityCard) -> set[str]:
    """Profile URLs an anchor implies, in the shapes a page would link to."""
    urls: set[str] = set()
    if card.x_handle:
        handle = card.x_handle.lower()
        urls.update({f"x.com/{handle}", f"twitter.com/{handle}"})
    github = card.anchors.get("github", "")
    if github:
        urls.add(f"github.com/{github.lower()}")
    return urls


def _anchor_handles(card: IdentityCard) -> set[str]:
    return {v.lower() for k, v in card.anchors.items() if k in ("x", "github") and v}


def links_to_anchor(facts: PageFacts, card: IdentityCard) -> str:
    """Does the page link to something we already know is theirs?

    Their blog linking their GitHub is near-proof, and it is the single most
    valuable thing a fetched page can show — the one signal a stranger's page
    is very unlikely to produce by accident.
    """
    anchor_hosts = card.anchor_hosts()
    profiles = _anchor_urls(card)
    for link in facts.links:
        host = _host(link)
        if host and host in anchor_hosts:
            return link
        trimmed = re.sub(r"^https?://(?:www\.)?", "", link.lower()).rstrip("/")
        for profile in profiles:
            if trimmed == profile or trimmed.startswith(profile + "/"):
                return link
    return ""


def _occurrences(pattern: re.Pattern[str] | None, text: str) -> list[re.Match[str]]:
    return list(pattern.finditer(text)) if pattern else []


# --------------------------------------------------------------------------
# negatives
# --------------------------------------------------------------------------


def _rejecting_host(host: str) -> Negative | None:
    """Hosts that disqualify a page before anything else is considered."""
    match = suffix_match(host, PEOPLE_SEARCH_HOSTS)
    if match:
        return Negative(
            "people_search",
            "reject",
            f"{match} is a people-search or data-broker site, which this tool never reads",
        )
    match = suffix_match(host, DIRECTORY_HOSTS)
    if match:
        return Negative(
            "directory",
            "reject",
            f"{match} is a directory or aggregator, not somewhere they publish",
        )
    match = suffix_match(host, tuple(REFUSED_HOSTS))
    if match:
        return Negative("refused_platform", "reject", f"not read — {REFUSED_HOSTS[match]}")
    match = suffix_match(host, INFRA_HOSTS)
    if match:
        return Negative("infrastructure", "reject", f"{match} hosts badges and boilerplate")
    return None


def _employer_negative(
    facts: PageFacts, card: IdentityCard, pattern: re.Pattern[str] | None, employer_matched: bool
) -> Negative | None:
    """Does the page attach a *different* employer to this name?

    Resolved by the page also naming the right employer: a page that mentions
    two companies is a page that mentions two companies.
    """
    if not card.employer or employer_matched:
        return None
    want = _normalize_org(card.employer)
    if not want:
        return None
    for match in _occurrences(pattern, facts.text):
        window = facts.text[match.end() : match.end() + _CLAIM_WINDOW]
        for found in _ORG_AFTER_NAME.findall(window):
            other = _normalize_org(found)
            if other and not (other & want):
                return Negative(
                    "different_employer",
                    STRONG,
                    f"the page puts this name at '{' '.join(found.split())}', not {card.employer}",
                    resolved_by="the page naming their actual employer somewhere",
                )
    return None


def _location_negative(
    facts: PageFacts, card: IdentityCard, pattern: re.Pattern[str] | None, location_matched: bool
) -> Negative | None:
    if not card.location or location_matched:
        return None
    want = _normalize_org(card.location)
    for match in _occurrences(pattern, facts.text):
        window = facts.text[match.end() : match.end() + _CLAIM_WINDOW]
        for found in _BASED_IN.findall(window):
            other = _normalize_org(found)
            if other and want and not (other & want):
                return Negative(
                    "different_location",
                    MODERATE,
                    f"the page places this name in '{' '.join(found.split())}', "
                    f"not {card.location}",
                    resolved_by="the page naming their actual location",
                )
    return None


def _role_negative(
    facts: PageFacts, card: IdentityCard, pattern: re.Pattern[str] | None, role_matched: bool
) -> Negative | None:
    """A clearly different field for this name.

    Only fires when the card states a role — without one there is nothing to
    contradict, and guessing at someone's field from a word on a page is how a
    scorer starts inventing reasons.
    """
    if not card.role or role_matched:
        return None
    want = _normalize_role(card.role)
    if not want:
        return None
    for match in _occurrences(pattern, facts.text):
        window = facts.text[match.end() : match.end() + _CLAIM_WINDOW]
        for found in _ROLE_AFTER_NAME.findall(window):
            other = _normalize_role(found)
            if other and not (other & want):
                return Negative(
                    "different_field",
                    MODERATE,
                    f"the page describes this name as '{' '.join(found.split())}', "
                    f"which shares nothing with {card.role}",
                    resolved_by="the page naming their actual role",
                )
    return None


def _citation_only(facts: PageFacts, card: IdentityCard, matches: list[re.Match[str]]) -> bool:
    """Is every mention of them a credit for someone else's work?

    One citation among other mentions is normal. *Only* citations means the
    page is someone else's writing that happens to name them, which is not a
    source of their thinking however prominently they appear.
    """
    if not matches or facts.declares_author(card.name) or facts.has_byline(card.name):
        return False
    for match in matches:
        before = facts.text[max(0, match.start() - _CITATION_WINDOW) : match.start()].lower()
        if not any(cue in before for cue in CITATION_CUES):
            return False
    return True


def _about_not_by(
    facts: PageFacts, card: IdentityCard, matches: list[re.Match[str]], own_host: bool
) -> Negative | None:
    """A page about them rather than by them.

    Worth more care than it looks. A profile piece, an interview write-up, and
    a conference bio are all *about* the target and contain none of their
    reasoning — but they are also exactly the pages a name search returns
    first, and they read as relevant to any scorer looking only for the name.
    """
    if not matches or facts.declares_author(card.name) or facts.has_byline(card.name):
        return None
    if facts.declares_other_author(card.name):
        return Negative(
            "about_not_by",
            "reject",
            f"the page declares a different author ({facts.authors[0]}), so this is "
            "writing about them rather than theirs",
        )
    if not own_host:
        haystack = f"{facts.url} {facts.title}".lower()
        for marker in ABOUT_MARKERS:
            if marker in haystack:
                return Negative(
                    "about_not_by",
                    "reject",
                    f"the URL or title marks this as a page about a person ('{marker}')",
                )
    third_person = 0
    for match in matches:
        window = facts.text[match.end() : match.end() + 40].lower()
        if any(re.match(rf"\W*{verb}\b", window) for verb in _ABOUT_VERBS):
            third_person += 1
    if third_person >= 2:
        return Negative(
            "about_not_by",
            "reject",
            f"the page quotes them in the third person {third_person} times and claims "
            "no authorship, so it is about them rather than by them",
        )
    return None


# --------------------------------------------------------------------------
# the scorer
# --------------------------------------------------------------------------


def score_candidate(
    facts: PageFacts,
    card: IdentityCard,
    *,
    query: str = "",
) -> CandidateScore:
    """Score one candidate against the identity card.

    Pure: same inputs, same verdict, every time, with no network and no model.
    """
    score = CandidateScore(url=facts.url, outcome="held", fetched=facts.fetched, query=query)

    if card.is_excluded(facts.url):
        score.outcome = "rejected"
        score.negatives.append(
            Negative("excluded", "reject", "the identity card excludes this URL")
        )
        return score

    host = facts.host
    host_negative = _rejecting_host(host)
    if host_negative is not None:
        score.outcome = "rejected"
        score.negatives.append(host_negative)
        return score

    anchor_hosts = card.anchor_hosts()
    own_host = bool(host) and (
        host in anchor_hosts or any(host.endswith("." + a) for a in anchor_hosts)
    )

    # A profile page is an identity, not a document. It can corroborate the
    # card — and `verify.py` records that — but it is never a corpus source.
    if not own_host and suffix_match(host, PROFILE_HOSTS):
        score.outcome = "rejected"
        score.negatives.append(
            Negative(
                "profile_page",
                "reject",
                f"{host} carries profiles rather than writing; recorded as a signal, not a source",
            )
        )
        return score

    pattern = name_pattern(card.name)
    matches = _occurrences(pattern, f"{facts.title}\n{facts.text}")
    score.name_present = bool(matches) or facts.declares_author(card.name)

    haystack = facts.haystack
    employer_matched = bool(card.employer) and bool(
        _normalize_org(card.employer) & _normalize_org(haystack)
    )
    role_matched = bool(card.role) and bool(_normalize_role(card.role) & _normalize_role(haystack))
    location_matched = bool(card.location) and card.location.strip().lower() in haystack

    # ---- negatives first -------------------------------------------------
    # Checked before the positives are counted, because a page that
    # contradicts the card is not made trustworthy by also agreeing with it in
    # two places.
    if _citation_only(facts, card, matches):
        score.outcome = "rejected"
        score.negatives.append(
            Negative(
                "citation_only",
                "reject",
                "their name appears only as a credit for someone else's work",
            )
        )
        return score

    about = _about_not_by(facts, card, matches, own_host)
    if about is not None:
        score.outcome = "context"
        score.negatives.append(about)
        return score

    for negative in (
        _employer_negative(facts, card, pattern, employer_matched),
        _location_negative(facts, card, pattern, location_matched),
        _role_negative(facts, card, pattern, role_matched),
    ):
        if negative is not None:
            score.negatives.append(negative)

    # ---- positives -------------------------------------------------------
    if own_host:
        score.signals.append(
            Signal("anchor_domain", STRONG, f"{host} is a host the card already anchors")
        )

    linked = links_to_anchor(facts, card)
    if linked:
        score.signals.append(Signal("links_to_anchor", STRONG, f"the page links to {linked}"))

    if facts.fetched and facts.declares_author(card.name):
        score.signals.append(
            Signal(
                "author_metadata",
                STRONG,
                f"structured author metadata names them ({facts.authors[0]})",
            )
        )
    elif facts.fetched and facts.has_byline(card.name):
        score.signals.append(Signal("byline_name", MODERATE, "their name is in the byline"))

    declared_handles = {h.lower() for h in facts.handles}
    handles = _anchor_handles(card)
    co_occurring = {h for h in handles if h in declared_handles or f"@{h}" in haystack}
    if len(co_occurring) >= 2:
        score.signals.append(
            Signal(
                "two_handles",
                STRONG,
                f"two anchor handles appear together on the page ({', '.join(sorted(co_occurring))})",
            )
        )
    elif co_occurring and declared_handles & handles:
        score.signals.append(
            Signal(
                "declared_handle",
                MODERATE,
                f"the page declares @{sorted(declared_handles & handles)[0]} as its author's handle",
            )
        )

    if employer_matched:
        score.signals.append(Signal("employer", MODERATE, f"{card.employer} is named on the page"))
    if role_matched:
        score.signals.append(Signal("role", MODERATE, f"the role matches ({card.role})"))
    if location_matched:
        score.signals.append(Signal("location", WEAK, f"{card.location} is named on the page"))

    # ---- the tier --------------------------------------------------------
    blocking = [n for n in score.negatives if n.effect != "reject"]
    if blocking:
        # A demotion, not merely a failure to promote: however many positives
        # agreed, an unresolved contradiction sends this to a human.
        score.outcome = "held"
    elif count_independent(score.signals) >= CORROBORATION_THRESHOLD:
        score.outcome = "corroborated"
    else:
        score.outcome = "held"
    return score
