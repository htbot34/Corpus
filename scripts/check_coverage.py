#!/usr/bin/env python3
"""Enforce per-module coverage floors on the paths that handle money and history.

A single total threshold is easy to satisfy for the wrong reason: a well-covered
rendering module can carry an under-tested budget module over the line, and the
number goes green while the code that spends your money stays untested. So the
floors are per file, and only for the four modules where being wrong is
expensive:

    budget.py   — every charge, reservation, and refusal
    ingest.py   — the walk that decides how much history you get
    client.py   — normalization and the caching that stops double-paying
    hydrate.py  — the context that makes a reply mean anything

Coverage elsewhere is welcome but not gated.

    pytest --cov=... --cov-report=json:coverage.json
    python scripts/check_coverage.py coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOORS: dict[str, float] = {
    "corpus/budget.py": 90.0,
    "corpus/x/ingest.py": 90.0,
    "corpus/x/client.py": 90.0,
    "corpus/x/hydrate.py": 90.0,
}


def main(argv: list[str]) -> int:
    report_path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    if not report_path.exists():
        print(
            f"{report_path} not found. Run:\n"
            "  pytest --cov=corpus.budget --cov=corpus.x.ingest --cov=corpus.x.client "
            "--cov=corpus.x.hydrate --cov-report=json:coverage.json",
            file=sys.stderr,
        )
        return 2

    data = json.loads(report_path.read_text(encoding="utf-8"))
    files = data.get("files", {})
    # Coverage keys paths as given to pytest, which may be absolute or relative.
    by_suffix = {Path(k).as_posix(): v for k, v in files.items()}

    failures: list[str] = []
    print(f"{'module':<24} {'covered':>8} {'floor':>7}")
    print("-" * 42)
    for module, floor in sorted(FLOORS.items()):
        match = next(
            (v for k, v in by_suffix.items() if k.endswith(module)),
            None,
        )
        if match is None:
            failures.append(f"{module}: not present in the coverage report")
            print(f"{module:<24} {'MISSING':>8} {floor:>6.0f}%")
            continue
        percent = float(match["summary"]["percent_covered"])
        mark = "" if percent >= floor else "  <-- below floor"
        print(f"{module:<24} {percent:>7.1f}% {floor:>6.0f}%{mark}")
        if percent < floor:
            missing = match["summary"].get("missing_lines", "?")
            failures.append(
                f"{module}: {percent:.1f}% is below the {floor:.0f}% floor "
                f"({missing} lines uncovered)"
            )

    print()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("all money-and-history modules meet their coverage floor")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
