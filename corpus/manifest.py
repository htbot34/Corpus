"""Run manifest: resume instead of restarting.

A 10,000-post run that dies at post 8,000 should not start over. The permanent
cache already makes re-fetching cheap — the tweets are on disk and a second pass
pays nothing for them — but it does not make re-*walking* free: the window loop
still issues every search request again, and search pages are billed per tweet
returned whether or not we have seen them before. The cache is keyed by tweet
id, and a search response is not a tweet id.

So the manifest records position, not content:

* the ingestion frontier (`until_ts`) and the windows already walked
* how many documents hydration has produced
* which map slices completed, and their results

Map slice results are held here rather than only in memory because the reduce
stage is the most likely thing to fail — a validation retry, a refusal, a budget
stop — and it is downstream of every map call. Re-paying the entire map stage to
retry one reduce is the single most expensive avoidable mistake this tool can
make.

The manifest is written next to the outputs (`out/{handle}/{date}/run.json`) so
it is obvious what it belongs to and gets cleaned up with them.

It is deliberately not a general checkpointing system. It answers one question —
"where had we got to?" — and is safe to delete at any time.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "run.json"
MANIFEST_VERSION = 1


@dataclass
class RunManifest:
    """Where a run had got to. Written after each stage, read by --resume."""

    handle: str = ""
    run_id: str = ""
    version: int = MANIFEST_VERSION
    created_at: str = ""
    updated_at: str = ""

    # -- ingestion --------------------------------------------------------
    # The frontier the window walk had reached. Resuming continues from here
    # rather than from "now".
    until_ts: int | None = None
    since_ts: int | None = None
    windows_walked: list[tuple[str, str]] = field(default_factory=list)
    ingest_complete: bool = False
    ingest_stats: dict[str, Any] = field(default_factory=dict)
    raw_tweet_ids: list[str] = field(default_factory=list)

    # -- hydration --------------------------------------------------------
    hydrate_complete: bool = False
    hydrated_documents: int = 0

    # -- synthesis --------------------------------------------------------
    # Completed slices, keyed by slice index as a string (JSON object keys are
    # strings, and round-tripping ints through JSON silently converts them —
    # better to be honest about it than to be surprised by it).
    map_slices: dict[str, Any] = field(default_factory=dict)
    map_total: int = 0
    reduce_complete: bool = False

    # -- accounting -------------------------------------------------------
    # Spend carried over from previous attempts, so a resumed run's budget is
    # measured against everything the target has cost, not just this attempt.
    prior_spend: float = 0.0

    def touch(self) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now

    # -- persistence ------------------------------------------------------

    def save(self, directory: Path) -> Path:
        """Write atomically.

        A manifest half-written by a crash is worse than no manifest: it would
        be read on the next attempt and believed. Write to a temp file in the
        same directory, then rename — which is atomic on every filesystem this
        runs on.
        """
        self.touch()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / MANIFEST_NAME
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".run-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(self), handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def load(cls, directory: Path) -> "RunManifest | None":
        """Read a manifest, or None if there is nothing usable there.

        A corrupt or future-versioned manifest returns None rather than raising:
        the correct response to "I cannot understand where we were" is to start
        over, not to refuse to run.
        """
        path = directory / MANIFEST_NAME if directory.is_dir() else directory
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            return None
        known = {f for f in cls.__dataclass_fields__}
        manifest = cls(**{k: v for k, v in data.items() if k in known})
        # JSON has no tuples; windows_walked round-trips as lists.
        manifest.windows_walked = [tuple(w) for w in manifest.windows_walked]  # type: ignore[misc]
        return manifest

    # -- queries ----------------------------------------------------------

    @property
    def completed_slice_indices(self) -> set[int]:
        return {int(k) for k in self.map_slices}

    def slice_result(self, index: int) -> Any | None:
        return self.map_slices.get(str(index))

    def record_slice(self, index: int, payload: Any) -> None:
        self.map_slices[str(index)] = payload

    def describe(self) -> str:
        """One line for the log, so a resume says what it is skipping."""
        parts = []
        if self.ingest_complete:
            parts.append(f"ingest complete ({len(self.raw_tweet_ids)} posts)")
        elif self.until_ts:
            frontier = datetime.fromtimestamp(self.until_ts, tz=timezone.utc)
            parts.append(
                f"ingest reached {frontier:%Y-%m-%d} ({len(self.raw_tweet_ids)} posts so far)"
            )
        if self.hydrate_complete:
            parts.append(f"hydrate complete ({self.hydrated_documents} documents)")
        if self.map_slices:
            parts.append(f"{len(self.map_slices)}/{self.map_total or '?'} map slices done")
        if self.reduce_complete:
            parts.append("reduce complete")
        if self.prior_spend:
            parts.append(f"${self.prior_spend:.4f} already spent")
        return "; ".join(parts) or "nothing completed yet"
