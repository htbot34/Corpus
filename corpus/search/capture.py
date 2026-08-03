"""Raw search capture: `--capture-search DIR`.

The same argument as `--capture-raw`, applied to the other vendor: fixtures
written from documentation prove the code is self-consistent and nothing more.
This writes what the search API actually returned, before `results_from_message`
interprets any of it, so a fixture can be built from evidence rather than from
a reading of the docs.

One JSON file per query, named `search_{slug}_{timestamp}.json`.

What is captured and why:

* **The whole message, verbatim.** Including the content blocks this code
  ignores. The interesting failures are the shapes we did not expect —
  `web_search_tool_result` carrying an error object where a list belongs,
  citations attached somewhere other than a text block — and a capture filtered
  to what the parser already understands cannot show any of them.
* **The parsed results alongside.** So a reader can see what the parser made of
  the payload without re-running it, which is the whole point when the two
  disagree.
* **Usage.** `server_tool_use.web_search_requests` is the billing unit; a
  capture that dropped it could not settle an argument about a bill.

`encrypted_content` is dropped from captured results. It is a multi-kilobyte
opaque blob per result, meaningful only when replayed to the same API, and
keeping it would make a capture directory unreadable for no gain. The raw
message keeps whatever it arrived with.

Capture never fails a run. A full disk costs a warning, not the search that was
already paid for.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..redact import MASK, secret_values

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .providers import SearchUsage

_SLUG = re.compile(r"[^a-z0-9]+")
_counter = itertools.count()


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.lower()).strip("_")[:60] or "query"


def _timestamp() -> str:
    # No colons: capture directories get read on Windows, where ':' is illegal
    # in a filename and the write would fail at the worst moment.
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S_%f")


def _jsonable(value: Any) -> Any:
    """Best-effort plain-data view of an SDK object.

    Pydantic models expose `model_dump`; anything else falls back to its repr
    rather than being dropped. A capture that silently omits a block it could
    not serialize is worse than one that records it awkwardly.
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class SearchCapture:
    """Writes raw search responses to `directory`."""

    def __init__(
        self,
        directory: Path | str,
        log: Callable[[str], None] = lambda _msg: None,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.log = log
        self.count = 0
        self.directory.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        query: str,
        provider: str,
        model: str,
        message: Any,
        results: list[dict[str, Any]],
        usage: SearchUsage | None = None,
    ) -> Path | None:
        """Write one search. Returns the path written, or None if it could not be."""
        target = self.directory / f"search_{_slug(query)}_{_timestamp()}_{next(_counter):04d}.json"
        envelope: dict[str, Any] = {
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "query": query,
            "provider": provider,
            "model": model,
            "usage": {
                "searches": usage.searches if usage else 0,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "cache_read_tokens": usage.cache_read_tokens if usage else 0,
                "cache_write_tokens": usage.cache_write_tokens if usage else 0,
                "errors": list(usage.errors) if usage else [],
            },
            "parsed_results": results,
            "raw_message": _jsonable(message),
        }

        try:
            serialized = json.dumps(envelope, indent=2, ensure_ascii=False, default=repr)
        except (TypeError, ValueError) as exc:
            self.log(f"  [capture-search] could not serialize {query!r}: {exc}")
            return None

        # Exact-value scan only, for the same reason the X capture uses one:
        # running credential-syntax patterns over captured page text would
        # rewrite ordinary prose — anyone quoting `Authorization: Bearer ...` on
        # their blog — and corrupt the evidence the capture exists to provide.
        # Exact values are unambiguous; patterns are guesses.
        for secret in secret_values():
            if secret and secret in serialized:
                serialized = serialized.replace(secret, MASK)

        try:
            target.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            self.log(f"  [capture-search] could not write {target.name}: {exc}")
            return None
        self.count += 1
        return target
