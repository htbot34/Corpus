"""Secret redaction for anything that can reach a log line, an exception, or disk.

The threat is mundane and therefore likely: an upstream 4xx echoes the request
back including the ``X-API-Key`` header, or a proxy rewrites the base URL to
carry the key as a query parameter, and the tool faithfully prints it into a
terminal, a CI log, or a pasted bug report.

Two layers, applied in order:

1. **Exact values.** Every secret-looking environment variable actually set in
   this process is substituted out by value. This catches a key no matter what
   shape it arrived in — header echo, URL, JSON body, stack trace.
2. **Patterns.** Known key prefixes and the usual credential-bearing syntax, so
   a secret that came from somewhere other than the environment (a config file,
   an upstream's own key in its error body) is still caught.

Redaction is a display concern, never a control-flow one: it must never raise,
and it must never change the length or meaning of a message beyond the
substitution itself.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable, Match, Union

MASK = "***REDACTED***"

# Env vars whose *values* must never appear in output. Named explicitly rather
# than inferred, plus a suffix rule so a provider added later is covered without
# anyone having to remember to update this list.
_SECRET_ENV_NAMES = frozenset(
    {
        "X_API_KEY",
        "ANTHROPIC_API_KEY",
        "APIDANCE_KEY",
        "SOCIALDATA_KEY",
    }
)
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")

# Below this length a "secret" is either a placeholder or a value so short that
# substituting it would corrupt unrelated text (e.g. X_API_KEY="1").
_MIN_SECRET_LEN = 8

_Replacement = Union[str, Callable[[Match[str]], str]]

_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    # Anthropic key prefix. Split so this file never contains the literal and
    # therefore never trips scripts/check_secrets.sh on itself.
    (re.compile(r"sk-" + r"ant-[A-Za-z0-9_-]{4,}"), "sk-" + "ant-" + MASK),
    # twitterapi.io key prefix, same trick.
    (re.compile(r"new" + r"1_[A-Za-z0-9]{4,}"), "new" + "1_" + MASK),
    # Credential-bearing query parameters, e.g. ...?apiKey=abc123&cursor=x
    (
        re.compile(
            r"([?&](?:api[_-]?key|apikey|key|token|access[_-]?token|auth|secret)=)[^&\s\"'>]+",
            re.IGNORECASE,
        ),
        r"\1" + MASK,
    ),
    # Bearer tokens anywhere. Must run BEFORE the header rule below: otherwise
    # "Authorization: Bearer <token>" has its *scheme* masked and its token left
    # in the clear, which looks redacted and is not.
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1" + MASK),
    # Header or mapping syntax, e.g. 'X-API-Key': 'abc123' / X_API_KEY=abc123.
    # Group 2 optionally absorbs an auth scheme word so "Authorization: Basic x"
    # masks x rather than "Basic".
    (
        re.compile(
            r"((?:x[-_]?api[-_]?key|authorization|api[-_]?key|apikey)"
            r"[\"']?\s*[:=]\s*[\"']?)([A-Za-z]+\s+)?(?!\s)([^\s\"',;}\)]{4,})",
            re.IGNORECASE,
        ),
        lambda m: m.group(1) + (m.group(2) or "") + MASK,
    ),
)


def secret_values(environ: dict[str, str] | None = None) -> list[str]:
    """Every secret value set in the environment, longest first.

    Longest-first matters: if two secrets share a prefix, replacing the short
    one first would leave the tail of the long one exposed.

    Read at call time, not import time, so a key set after startup — or unset by
    a test — is handled correctly.
    """
    env = os.environ if environ is None else environ
    values: set[str] = set()
    for name, value in env.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        upper = name.upper()
        if upper in _SECRET_ENV_NAMES or upper.endswith(_SECRET_ENV_SUFFIXES):
            values.add(value)
    return sorted(values, key=len, reverse=True)


def redact(value: Any, extra: Iterable[str] = ()) -> str:
    """Return ``value`` as a string with every known secret masked.

    Never raises: a redaction failure must not become the error the user sees
    instead of the real one.
    """
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - a __str__ that throws
        return "<unprintable>"

    try:
        for secret in list(extra) + secret_values():
            if secret and len(secret) >= _MIN_SECRET_LEN and secret in text:
                text = text.replace(secret, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
    except Exception:  # pragma: no cover - defensive; never break error reporting
        return MASK
    return text


class RedactingError(RuntimeError):
    """Base for exceptions whose message may quote an upstream response.

    Redaction happens at construction rather than at ``str()`` time so that the
    unredacted string never exists on the exception at all — nothing downstream
    can reach around it via ``.args``, and a bare ``repr(exc)`` in a traceback is
    as safe as a deliberate ``str(exc)``.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*(redact(a) if isinstance(a, str) else a for a in args))
