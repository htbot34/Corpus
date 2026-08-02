"""Worldview axes: the configurable list the reduce stage places the subject on.

Loaded from `profiles.yaml` rather than hardcoded, because which axes matter
depends on who you are reading. `--axes a,b` restricts a run; every selected
axis still appears in the report, with `signal: "none"` when the corpus cannot
speak to it. An axis silently missing from the output would be indistinguishable
from an axis the subject never touched, which is the whole thing we are trying
to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

PROFILES_PATH = Path(__file__).with_name("profiles.yaml")


class AxisError(ValueError):
    """Raised for an unknown axis name or an unusable profiles.yaml."""


@dataclass(frozen=True)
class AxisSpec:
    name: str
    label: str
    probe: str

    def as_prompt_line(self) -> str:
        return f"- {self.name} ({self.label}): {' '.join(self.probe.split())}"


def load_axes(path: Path | None = None) -> list[AxisSpec]:
    """Every axis defined in profiles.yaml, in file order."""
    path = path or PROFILES_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise AxisError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise AxisError(f"{path} is not valid YAML: {exc}") from exc

    entries = raw.get("axes")
    if not isinstance(entries, list) or not entries:
        raise AxisError(f"{path} must define a non-empty `axes:` list")

    specs: list[AxisSpec] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise AxisError(f"{path}: every axis needs a `name`")
        name = str(entry["name"]).strip()
        specs.append(
            AxisSpec(
                name=name,
                label=str(entry.get("label") or name.replace("_", " ")),
                probe=str(entry.get("probe") or ""),
            )
        )
    return specs


def select_axes(selection: str | None, path: Path | None = None) -> list[AxisSpec]:
    """Resolve `--axes a,b,c` against profiles.yaml. Empty selection means all.

    Unknown names are an error naming the valid ones rather than a silent drop:
    a typo that quietly removes an axis would produce a report that looks
    complete and is not.
    """
    available = load_axes(path)
    if not selection or not selection.strip():
        return available

    by_name = {spec.name: spec for spec in available}
    chosen: list[AxisSpec] = []
    unknown: list[str] = []
    for token in selection.split(","):
        key = token.strip()
        if not key:
            continue
        spec = by_name.get(key)
        if spec is None:
            unknown.append(key)
        elif spec not in chosen:
            chosen.append(spec)

    if unknown:
        raise AxisError(
            f"unknown axis {', '.join(repr(u) for u in unknown)}. Valid axes: {', '.join(by_name)}"
        )
    if not chosen:
        raise AxisError("--axes was given but selected nothing")
    return chosen


def axes_prompt_block(specs: list[AxisSpec]) -> str:
    lines = [spec.as_prompt_line() for spec in specs]
    return "\n".join(lines)
