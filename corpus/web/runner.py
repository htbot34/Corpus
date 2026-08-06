"""Build and execute the exact CLI invocation a terminal user would type.

This is the whole of the web layer's relationship to the pipeline: it
assembles an argv for `corpus profile` / `corpus run` and streams the
subprocess's output, line by line, into the run log. Nothing is
reimplemented — the budget pre-flight, the dry-run estimate, the redaction
of secrets in output, and every error message are the CLI's own, because
they are produced by the CLI. A failure arrives here as the same text a
terminal user would have seen, in full.

The subprocess inherits the environment, so ANTHROPIC_API_KEY, X_API_KEY
and GITHUB_TOKEN come from the same `.env` the CLI reads. The keys are
never written to the log: the CLI redacts its own output at the source
(`corpus/redact.py`), and this module never prints the environment.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LineSink = Callable[[str], None]

#: "Report: out/<key>/<date>/report.md", printed by `corpus run` at the end.
_REPORT_LINE = re.compile(r"^Report:\s+(.+?)report\.md\s*$")
#: "Total:   $1.2345 of $10.00 budget", from Budget.summary_lines().
_TOTAL_LINE = re.compile(r"^\s*Total:\s+\$([0-9.]+)\s+of\b")


class FormError(ValueError):
    """A form submission the server refuses, with the reason for the user."""


@dataclass
class RunParams:
    """Everything the new-run form collects. No secrets — ever.

    `save_profile` is False when the user picked an existing saved target,
    in which case only the key matters and the card on file wins.
    """

    key: str
    name: str
    x: str = ""
    github: str = ""
    site: str = ""
    employer: str = ""
    role: str = ""
    budget: float = 10.0
    highlights: int = 60
    save_profile: bool = True

    @classmethod
    def from_form(cls, form: Mapping[str, str]) -> RunParams:
        """Validate a form the way the CLI validates flags: loudly."""
        existing = (form.get("target") or "").strip()
        if existing:
            return cls(
                key=existing,
                name=(form.get("name") or "").strip(),
                budget=_positive_float(form, "budget", 10.0),
                highlights=_positive_int(form, "highlights", 60),
                save_profile=False,
            )
        key = (form.get("key") or "").strip()
        name = (form.get("name") or "").strip()
        if not key or not name:
            raise FormError("name and key are required; everything else is optional")
        return cls(
            key=key,
            name=name,
            x=(form.get("x") or "").strip().lstrip("@"),
            github=(form.get("github") or "").strip(),
            site=(form.get("site") or "").strip(),
            employer=(form.get("employer") or "").strip(),
            role=(form.get("role") or "").strip(),
            budget=_positive_float(form, "budget", 10.0),
            highlights=_positive_int(form, "highlights", 60),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "x": self.x,
            "github": self.github,
            "site": self.site,
            "employer": self.employer,
            "role": self.role,
            "budget": self.budget,
            "highlights": self.highlights,
            "save_profile": self.save_profile,
        }

    @classmethod
    def from_json(cls, raw: str) -> RunParams:
        data = json.loads(raw)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def _positive_float(form: Mapping[str, str], field_name: str, default: float) -> float:
    raw = (form.get(field_name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise FormError(f"{field_name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise FormError(f"{field_name} must be positive, got {raw!r}")
    return value


def _positive_int(form: Mapping[str, str], field_name: str, default: int) -> int:
    raw = (form.get(field_name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise FormError(f"{field_name} must be a whole number, got {raw!r}") from exc
    if value <= 0:
        raise FormError(f"{field_name} must be positive, got {raw!r}")
    return value


# -- argv builders ---------------------------------------------------------


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "corpus.cli", *args]


def profile_argv(params: RunParams) -> list[str]:
    args = _cli("profile", "--key", params.key, "--name", params.name)
    for flag, value in (
        ("--x", params.x),
        ("--github", params.github),
        ("--site", params.site),
        ("--employer", params.employer),
        ("--role", params.role),
    ):
        if value:
            args += [flag, value]
    return args


def run_argv(params: RunParams, *, dry_run: bool) -> list[str]:
    args = _cli(
        "run",
        "--target",
        params.key,
        "--budget",
        f"{params.budget:.2f}",
        "--highlights",
        str(params.highlights),
    )
    # The gate: the preview path passes --dry-run and can never spend; the
    # worker passes --yes because the human already confirmed on the estimate
    # screen — the web page IS the confirmation prompt.
    args.append("--dry-run" if dry_run else "--yes")
    return args


# -- execution -------------------------------------------------------------


def stream_command(argv: list[str], sink: LineSink) -> int:
    """Run one CLI command, feeding each output line to `sink` as it appears.

    stderr merges into stdout so a traceback lands in the run log verbatim,
    in order, exactly as a terminal would have shown it.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        sink(raw.rstrip("\n"))
    return proc.wait()


def execute_dry_run(params: RunParams) -> tuple[int, str]:
    """Save the profile (free), then run the CLI's own --dry-run path.

    Returns (exit_code, full output). Nothing here can spend: --dry-run
    stops before any paid fetch, in the CLI's own words.
    """
    lines: list[str] = []
    if params.save_profile:
        code = stream_command(profile_argv(params), lines.append)
        if code != 0:
            return code, "\n".join(lines)
    code = stream_command(run_argv(params, dry_run=True), lines.append)
    return code, "\n".join(lines)


@dataclass
class RunOutcome:
    """What the worker records about a finished run."""

    exit_code: int
    out_dir: str = ""
    spend: float = 0.0
    documents: int = 0
    tier: str = ""
    #: The run completed cleanly and the corpus was empty — the CLI's
    #: "finding, not a failure" outcome. Distinct from a failure and from a
    #: synthesized run, and displayed as its own status.
    empty_corpus: bool = False
    notes: list[str] = field(default_factory=list)


def execute_run(params: RunParams, sink: LineSink) -> RunOutcome:
    """The paid run: the same `corpus run` a terminal user would type."""
    seen: list[str] = []

    def tee(line: str) -> None:
        seen.append(line)
        sink(line)

    code = stream_command(run_argv(params, dry_run=False), tee)
    outcome = RunOutcome(exit_code=code)
    for line in seen:
        report = _REPORT_LINE.match(line.strip())
        if report:
            outcome.out_dir = str(Path(report.group(1).strip()))
        total = _TOTAL_LINE.match(line)
        if total:
            outcome.spend = float(total.group(1))
    if outcome.out_dir:
        _read_run_meta(outcome)
    return outcome


def _read_run_meta(outcome: RunOutcome) -> None:
    """Tier and document count from the run's own metadata, best-effort.

    Display bookkeeping only — a parse failure here must never turn a
    finished run into a failed one, so it is noted rather than raised.
    """
    meta_path = Path(outcome.out_dir) / "run_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        outcome.documents = int(meta.get("analyzed_documents") or 0)
        outcome.tier = str(meta.get("corpus_tier") or "")
        outcome.empty_corpus = bool(meta.get("empty_corpus"))
    except (OSError, ValueError) as exc:
        outcome.notes.append(f"could not read {meta_path}: {exc}")
