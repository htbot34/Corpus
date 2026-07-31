"""Raw wire capture: `--capture-raw DIR`.

Phase 1's premise is that fixtures written from documentation prove only that
the code is self-consistent. This module exists to replace assumption with
evidence: it writes every provider response to disk exactly as it arrived,
*before* `_tweets_from`, `_cursor_from`, or `normalize_tweet` touch it, so the
captured file is a statement about the provider and not about our parsing of it.

One JSON file per call, named ``{endpoint}_{timestamp}.json``.

Three properties are deliberate:

* **Verbatim body.** The response body is parsed with ``json.loads`` (which
  preserves key order) and re-emitted without reordering, filtering, or
  renaming. ``body_sha256`` is taken over the original bytes, so anyone can
  prove the file was not quietly normalized on the way to disk.
* **Response headers included.** Not decoration: ``Retry-After`` and
  ``x-rate-limit-reset`` are what Phase 2.4's retry logic needs to honour, and
  the only way to learn their real names and formats is to look at a capture.
* **Request headers excluded.** That is where the API key lives. Query params
  and response headers are passed through ``redact()`` on the way out, because
  a capture directory is exactly the kind of thing that gets zipped and shared.

The body gets a narrower treatment than the rest of the envelope: *exact secret
values only*, never the structural patterns. An upstream 401 that echoes the key
back in its body is a real and observed shape, so the body cannot be trusted
blindly — but running credential-syntax regexes over captured tweet text would
rewrite ordinary posts (anyone quoting ``Authorization: Bearer …``) and corrupt
the very evidence the capture exists to provide. Exact values are unambiguous;
patterns are guesses. When a body is touched, ``body_redacted`` says so, because
``body_sha256`` is taken over the original bytes and will no longer match.

Capture never fails a run. A full disk or a bad path costs a warning, not the
data the user already paid to fetch.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..redact import MASK, redact, secret_values

_SLUG = re.compile(r"[^a-z0-9]+")

# Same-microsecond collisions are unlikely and the cost of preventing them is
# one integer, so prevent them rather than lose a capture to an overwrite.
_counter = itertools.count()


def _slug(path: str) -> str:
    return _SLUG.sub("_", path.lower()).strip("_") or "response"


def _timestamp() -> str:
    # No colons: capture directories get read on Windows, where ':' is illegal
    # in a filename and the write would fail silently at the worst moment.
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S_%f")


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {str(k): redact(v) for k, v in mapping.items()}


class RawCapture:
    """Writes raw provider responses to `directory`."""

    def __init__(
        self,
        directory: Path | str,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.log = log
        self.count = 0
        self._extra_secrets: set[str] = set()
        self.directory.mkdir(parents=True, exist_ok=True)

    def add_secret(self, value: str | None) -> None:
        """Register a credential this capture must never write.

        ``secret_values()`` covers keys that came from the environment, which is
        the normal path. A provider constructed with an explicit ``api_key=``
        argument — programmatic use, and every test in this repo — would
        otherwise be invisible to it.
        """
        if value and len(value) >= 8:
            self._extra_secrets.add(value)

    def record(
        self,
        *,
        method: str,
        url: str,
        path: str,
        params: dict[str, Any],
        status_code: int,
        headers: dict[str, Any],
        body_bytes: bytes,
    ) -> Path | None:
        """Write one call. Returns the path written, or None if it could not be."""
        name = f"{_slug(path)}_{_timestamp()}_{next(_counter):04d}.json"
        target = self.directory / name

        # Parse for readability, but keep the exact bytes' digest so the file
        # can be proven un-normalized. A body that is not JSON is still worth
        # keeping verbatim — a non-JSON response IS a finding.
        try:
            body: Any = json.loads(body_bytes.decode("utf-8"))
            parse_error: str | None = None
        except Exception as exc:
            body = body_bytes.decode("utf-8", errors="replace")
            parse_error = f"{type(exc).__name__}: {exc}"

        # Exact-value scan only — see the module docstring for why patterns are
        # deliberately not applied here.
        body_text = body_bytes.decode("utf-8", errors="replace")
        known = set(secret_values()) | self._extra_secrets
        leaked = [s for s in known if s and s in body_text]

        envelope = {
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "request": {
                "method": method,
                # The full URL and the path separately: the wire contract needs
                # the exact path and the exact query parameter *names*, and a
                # normalized params dict alone would lose how they were sent.
                "url": redact(url),
                "path": path,
                "params": _redact_mapping(params),
            },
            "response": {
                "status": status_code,
                "headers": _redact_mapping(headers),
                # Digest of the bytes as they arrived. If body_redacted is
                # false, this proves the stored body is the wire payload.
                "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "body_bytes": len(body_bytes),
                "body_parse_error": parse_error,
                "body_redacted": bool(leaked),
                "body": body,
            },
        }

        serialized = json.dumps(envelope, indent=2, ensure_ascii=False)
        for secret in leaked:
            serialized = serialized.replace(secret, MASK)

        try:
            target.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            # Never let a capture problem cost a paid fetch.
            self.log(f"  [capture] could not write {target.name}: {exc}")
            return None
        self.count += 1
        return target
