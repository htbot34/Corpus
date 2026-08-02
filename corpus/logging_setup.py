"""Structured logging.

Replaces the `log: Callable[[str], None] = print` pattern that ran through
ingest, hydrate, synthesize, and the sources. That pattern was fine for a
one-shot: it made every module trivially testable by passing a list's `append`.
It was also unfilterable, untimestamped, and impossible to correlate across a
run — and a run that costs money and takes minutes needs all three.

Every record carries:

* **run_id** — the same id the spend table keys on, so a line in a terminal and
  a row in `corpus budget log` can be tied together after the fact.
* **phase** — ingest / hydrate / signals / map / reduce / render.
* **elapsed** — seconds since the run started. Wall-clock timestamps answer
  "when"; on a pipeline whose stages have wildly different costs, "how long into
  the run" is the question actually being asked.

Two formats. Text is the default and is meant to be read by a person watching a
run; JSON is one object per line for anything that needs to be grepped or
replayed. The per-window progress lines are preserved verbatim in text mode —
they are the most useful thing the tool prints.

The `log(str)` callable is kept as the interface the pipeline modules see. A
`logging.Logger` threaded through every function signature would be a larger,
noisier change than the problem justifies, and the callable keeps those modules
trivially testable. What changes is what sits behind it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

LOGGER_NAME = "corpus"

TEXT = "text"
JSON = "json"
LOG_FORMATS = (TEXT, JSON)


class RunContext(logging.Filter):
    """Stamps run_id, phase, and elapsed onto every record.

    A filter rather than an adapter so that records from anywhere — including a
    library that got hold of the logger — carry the fields, and the formatters
    can rely on them existing.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.phase = "startup"
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        record.phase = getattr(record, "phase", None) or self.phase
        record.elapsed = round(self.elapsed, 3)
        return True


class TextFormatter(logging.Formatter):
    """Human-readable, and deliberately close to the original output.

    Progress lines already start with two spaces and carry their own structure;
    prefixing them with a level and a phase would make a run harder to read, not
    easier. So INFO lines pass through unadorned unless --verbose asks for the
    context, and anything at WARNING or above is marked because it needs to be
    noticed.
    """

    def __init__(self, verbose: bool = False) -> None:
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            prefix = f"{record.levelname}: "
        elif self.verbose:
            prefix = f"[{record.elapsed:7.2f}s {record.phase:<8}] "
        else:
            prefix = ""
        return f"{prefix}{message}"


class JsonFormatter(logging.Formatter):
    """One object per line. For grepping, replaying, and shipping."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", ""),
            "phase": getattr(record, "phase", ""),
            "elapsed": getattr(record, "elapsed", 0.0),
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RunLogger:
    """The run's logging surface.

    Owns the logger, the context filter, and the `log(str)` callables handed to
    the pipeline modules.
    """

    def __init__(
        self,
        run_id: str,
        *,
        log_format: str = TEXT,
        verbose: bool = False,
        quiet: bool = False,
        stream: Any = None,
    ) -> None:
        if log_format not in LOG_FORMATS:
            raise ValueError(f"log format {log_format!r} is not one of {LOG_FORMATS}")
        self.run_id = run_id
        self.context = RunContext(run_id)
        self.logger = logging.getLogger(f"{LOGGER_NAME}.{run_id}")
        # Isolated from the root logger: this is a CLI, and inheriting whatever
        # handlers a library installed is how duplicate lines happen.
        self.logger.propagate = False
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)

        handler = logging.StreamHandler(stream or sys.stdout)
        handler.addFilter(self.context)
        handler.setFormatter(
            JsonFormatter() if log_format == JSON else TextFormatter(verbose=verbose)
        )
        # --quiet keeps warnings and errors. A quiet flag that hides a budget
        # stop or a truncated history is not quiet, it is broken.
        handler.setLevel(logging.WARNING if quiet else logging.DEBUG)
        self.logger.addHandler(handler)

    @property
    def phase(self) -> str:
        return self.context.phase

    @contextmanager
    def phase_scope(self, name: str) -> Iterator[None]:
        """Mark every record emitted inside as belonging to `name`."""
        previous = self.context.phase
        self.context.phase = name
        started = time.monotonic()
        try:
            yield
        finally:
            self.logger.debug(
                f"phase {name} finished in {time.monotonic() - started:.2f}s",
                extra={"phase": name},
            )
            self.context.phase = previous

    # -- the interface the pipeline modules take ---------------------------

    def sink(self, phase: str | None = None) -> Callable[[str], None]:
        """A `log(str)` callable, as ingest/hydrate/synthesize expect."""

        def emit(message: str = "") -> None:
            self.logger.info(message, extra={"phase": phase or self.context.phase})

        return emit

    def info(self, message: str, **fields: Any) -> None:
        self.logger.info(message, extra={"extra_fields": fields})

    def warning(self, message: str, **fields: Any) -> None:
        self.logger.warning(message, extra={"extra_fields": fields})

    def error(self, message: str, **fields: Any) -> None:
        self.logger.error(message, extra={"extra_fields": fields})

    def debug(self, message: str, **fields: Any) -> None:
        self.logger.debug(message, extra={"extra_fields": fields})

    def close(self) -> None:
        for handler in list(self.logger.handlers):
            handler.flush()
            self.logger.removeHandler(handler)
            handler.close()
