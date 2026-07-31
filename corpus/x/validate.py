"""Validation for user-supplied strings that reach a provider query.

`ingest.py` builds its search query by interpolation:

    f"from:{handle} since_time:{since_ts} until_time:{until_ts}"

after only `.lstrip("@")`. A handle containing whitespace or a search operator
therefore does not fail — it silently changes what the query *means*. The
provider returns an empty or nonsensical result set and the run reports "0
posts" as though the account were empty. Nothing anywhere says the query was
malformed.

X's own rule is 1-15 characters of `[A-Za-z0-9_]`, which excludes every
character that could alter query semantics, so validating to the real rule and
validating for safety turn out to be the same job.

Validation happens at the CLI boundary — as early as possible, where the error
can name the offending input and the user can still fix it — and again at the
ingestion boundary, because `ingest_timeline` is a public function that a
future caller might reach without going through the CLI.
"""

from __future__ import annotations

import re

# X's rule. Deliberately a whitelist: a blacklist of operator characters would
# need updating every time the provider adds a search operator.
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# Characters whose presence means the string would change query semantics
# rather than merely being invalid. Used only to explain *why* in the error.
_OPERATOR_HINTS = {
    " ": "a space starts a new search term",
    "\t": "whitespace starts a new search term",
    "\n": "a newline starts a new search term",
    ":": "':' introduces a search operator such as since_time:",
    '"': "a quote opens a phrase match",
    "(": "parentheses group search terms",
    ")": "parentheses group search terms",
    "-": "a leading '-' negates a term",
    "*": "'*' is a wildcard",
}


class InvalidHandle(ValueError):
    """A handle that would change the meaning of a search query, or is not a handle."""


def validate_handle(raw: str, *, field: str = "--x") -> str:
    """Return the normalized handle, or raise InvalidHandle naming the input.

    Normalization is only stripping a leading '@' and surrounding whitespace —
    anything else that differs from the input is rejected rather than silently
    corrected, because silently correcting a handle is how you analyse the
    wrong person.
    """
    if raw is None:
        raise InvalidHandle(f"{field} is required")
    handle = str(raw).strip().lstrip("@")

    if not handle:
        raise InvalidHandle(f"{field}: no handle given (got {raw!r})")

    if HANDLE_RE.match(handle):
        return handle

    reasons: list[str] = []
    if len(handle) > 15:
        reasons.append(f"X handles are at most 15 characters; this is {len(handle)}")
    offenders = sorted({c for c in handle if not re.match(r"[A-Za-z0-9_]", c)})
    for char in offenders:
        hint = _OPERATOR_HINTS.get(char)
        reasons.append(
            f"{char!r} is not allowed in an X handle"
            + (f" — {hint}" if hint else "")
        )

    raise InvalidHandle(
        f"{field}: {raw!r} is not a valid X handle. "
        + " ".join(reasons)
        + " The handle is interpolated into a search query, so an invalid one "
        "would not error — it would quietly change what is searched for and "
        "return a baffling empty result. Handles are 1-15 characters of "
        "letters, digits, and underscore."
    )


def validate_query_fragment(value: str, *, field: str) -> str:
    """Guard any other user string destined for a provider query.

    Nothing currently reaches a query besides the handle. This exists so that
    the next thing that does has an obvious place to be checked, rather than
    being interpolated because interpolation is what the surrounding code does.
    """
    text = str(value)
    bad = sorted({c for c in text if c in _OPERATOR_HINTS})
    if bad:
        details = "; ".join(f"{c!r}: {_OPERATOR_HINTS[c]}" for c in bad)
        raise InvalidHandle(
            f"{field}: {value!r} contains characters that would change the "
            f"meaning of the search query ({details})."
        )
    return text
