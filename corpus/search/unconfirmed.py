"""`unconfirmed.md`: the human in the loop.

Everything search found and could not prove lands here instead of in the
corpus. The format is a markdown checklist because the decision needs a person
and a person needs to make it in a text editor in thirty seconds — one line
per candidate, with the four things that decide it: what found it, what
matched, what did not, and what the page says.

The round trip is deliberately asymmetric, and the file says so at the top:

* **Checked** means "yes, this is them". It is ingested as `corroborated` with
  basis `user-confirmed` — a human is better evidence than any signal in
  `scoring.py`, which is why this path exists.
* **Unchecked** means "no". The URL goes into the card's `exclude` list so it
  never resurfaces, on this run or any later one.

That second rule makes an untouched file dangerous — every candidate would be
rejected forever by a file nobody edited. So `read_unconfirmed` reports how
many entries were checked, and the CLI refuses to act on a file where nothing
was, rather than quietly excluding everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..identity import IdentityCard
from .verify import SearchCandidate, SearchPhaseResult

_ENTRY = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.*)$")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_BACKTICKED = re.compile(r"`([^`]*)`")

HEADER = """# Unconfirmed sources for {subject}

_Generated {when}. {found} candidate(s) matched the name and could not be proved._

These are **not** in the corpus. Search found them; nothing about them was
strong enough to attribute with confidence, so a person decides.

**Tick the box** next to anything that really is theirs. On the next run with
`--accept-unconfirmed {path}`:

- **Checked** entries are ingested as `corroborated`, with the basis
  `user-confirmed`.
- **Unchecked** entries are written into the identity card's `exclude` list, so
  they never come back.

Leaving a line alone is therefore a decision, not an absence of one. If you
have not looked at these yet, do not pass this file to `--accept-unconfirmed`.
"""


@dataclass
class UnconfirmedEntry:
    """One line of the file, read back."""

    url: str
    checked: bool
    query: str = ""
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _query_of(value: str) -> str:
    """The query out of a `Found by:` line.

    The query is itself full of quotes, so it is written inside backticks and
    read back from between them. Stripping backticks off the ends instead
    would leave the `query ` label attached — and a query is compared against
    the generated ones, so a stray label makes every comparison fail quietly.
    """
    fenced = _BACKTICKED.search(value)
    if fenced is not None:
        return fenced.group(1).strip()
    return value.strip().removeprefix("query").strip()


def _wrap(label: str, values: list[str], empty: str) -> str:
    return f"      {label}: {'; '.join(values) if values else empty}"


def _entry_lines(candidate: SearchCandidate) -> list[str]:
    score = candidate.score
    matched = score.matched if score else []
    missing = score.missing if score else []
    lines = [f"- [ ] {candidate.url}"]
    if candidate.title:
        lines.append(f"      Title: {candidate.title}")
    if candidate.query:
        lines.append(f"      Found by: query `{candidate.query}`")
    lines.append(_wrap("Matched", matched, "nothing beyond the name"))
    lines.append(_wrap("Did not match", missing, "—"))
    if candidate.skipped:
        lines.append(f"      Note: {candidate.skipped}")
    if candidate.snippet:
        snippet = " ".join(candidate.snippet.split())[:300]
        lines.append(f"      Snippet: {snippet}")
    return lines


def render_unconfirmed(
    card: IdentityCard,
    result: SearchPhaseResult,
    path: Path | str = "unconfirmed.md",
) -> str:
    """The whole file, as text."""
    when = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        HEADER.format(
            subject=card.display,
            when=when,
            found=len(result.held),
            path=path,
        )
    ]

    if result.common_name:
        fields = ", ".join(f"`{f}`" for f in result.disambiguators) or "`employer`, `location`"
        out.append("")
        out.append(f"> **This name is ambiguous.** {result.notes[0] if result.notes else ''}")
        out.append(">")
        out.append(
            f"> Adding {fields} to the identity card would narrow it more than any "
            f"amount of extra searching. Set them with "
            f"`corpus profile --target {card.key} --employer ... --location ...`."
        )

    out.append("")
    if result.held:
        out.append("## Candidates")
        out.append("")
        for candidate in result.held:
            out.extend(_entry_lines(candidate))
            out.append("")
    else:
        out.append("_Nothing was held back on this run._")
        out.append("")

    if result.context:
        out.append("## About them, not by them")
        out.append("")
        out.append(
            "Recorded for context and **never ingested**, whatever you do here: a "
            "profile piece or an interview write-up contains the interviewer's "
            "prose, not the subject's reasoning."
        )
        out.append("")
        for candidate in result.context:
            reason = (
                candidate.score.negatives[0].detail
                if candidate.score and candidate.score.negatives
                else "about the subject"
            )
            out.append(f"- {candidate.url} — {reason}")
        out.append("")

    if result.rejected:
        out.append("## Rejected outright")
        out.append("")
        for candidate in result.rejected:
            reason = (
                candidate.score.negatives[0].detail
                if candidate.score and candidate.score.negatives
                else "rejected"
            )
            out.append(f"- {candidate.url} — {reason}")
        out.append("")

    return "\n".join(out)


def write_unconfirmed(
    directory: Path,
    card: IdentityCard,
    result: SearchPhaseResult,
) -> Path:
    """Write `unconfirmed.md` into a run directory. Returns the path."""
    path = Path(directory) / "unconfirmed.md"
    path.write_text(render_unconfirmed(card, result, path), encoding="utf-8")
    return path


def read_unconfirmed(path: Path) -> list[UnconfirmedEntry]:
    """Read a hand-edited file back.

    Tolerant on purpose: the file is meant to be edited by a person in a text
    editor, so indentation, `*` instead of `-`, and reordered or deleted
    context lines are all fine. The two things that matter are the checkbox and
    the URL, and a line without a URL is not an entry.
    """
    text = Path(path).read_text(encoding="utf-8")
    entries: list[UnconfirmedEntry] = []
    current: UnconfirmedEntry | None = None

    for line in text.splitlines():
        match = _ENTRY.match(line)
        if match:
            url_match = _URL.search(match.group(2))
            if url_match is None:
                current = None
                continue
            current = UnconfirmedEntry(
                url=url_match.group(0).rstrip(".,;)"),
                checked=match.group(1).lower() == "x",
            )
            entries.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Found by:"):
            current.query = _query_of(stripped.removeprefix("Found by:"))
        elif stripped.startswith("Matched:"):
            current.matched = [
                p.strip() for p in stripped.removeprefix("Matched:").split(";") if p.strip()
            ]
        elif stripped.startswith("Did not match:"):
            current.missing = [
                p.strip() for p in stripped.removeprefix("Did not match:").split(";") if p.strip()
            ]
        elif not stripped:
            current = None

    # Deduplicated on URL, keeping the first: a file that lists the same URL
    # twice with different boxes is ambiguous, and the safe reading of an
    # ambiguous accept is the earlier, more considered one.
    seen: set[str] = set()
    unique: list[UnconfirmedEntry] = []
    for entry in entries:
        if entry.url in seen:
            continue
        seen.add(entry.url)
        unique.append(entry)
    return unique
