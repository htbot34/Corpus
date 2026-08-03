"""The identity card: who the corpus is about, and what is known to be theirs.

The tool used to take an X handle and treat everything it found under that
handle as the subject's, which is trivially safe — a handle *is* an identity.
Discovery breaks that. Once the tool searches for a person by name it can find
other people, and a report that confidently attributes a stranger's blog post
to the subject is worse than no report, because the output is indistinguishable
from a correct one.

So the user supplies anchors — URLs and handles confirmed to be theirs — plus
whatever disambiguating facts they have (employer, role, location), and
everything discovered afterwards is scored against that card. The card is the
ground truth; discovery never adds to it on its own.

Anchors live in `profiles.yaml` under `targets:`, resolved in this order:

    $CORPUS_PROFILES  ->  ./profiles.yaml  ->  ~/.corpus/profiles.yaml

That is deliberately *not* `corpus/profiles.yaml`, which ships inside the
package and holds the worldview axes. Targets are personal notes about real
people; they do not belong in a wheel, and a `pip install -U` must not be able
to delete them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .x.validate import InvalidHandle, validate_handle

# The anchor kinds the tool knows how to follow. An unknown key is an error
# naming these, for the same reason an unknown axis name is: a typo that
# silently drops an anchor produces a report that looks complete and is not.
KNOWN_ANCHORS = ("x", "github", "site", "substack", "rss")

# Platforms we will not read, with the reason. Naming them here means the
# refusal happens when the anchor is written down rather than three phases
# later, in the middle of a paid run.
REFUSED_ANCHORS = {
    "linkedin": (
        "LinkedIn blocks automated access and forbids it in their terms. Copy the "
        "text you want by hand and add it as a local file source instead."
    ),
    "facebook": (
        "Personal Facebook profiles are not public data. A public Page via the "
        "Graph API with a token would be, but a profile is not."
    ),
    "instagram": "Instagram requires an authenticated session, which this tool never uses.",
}

# GitHub's own rule: alphanumeric or single hyphens, no leading or trailing
# hyphen, 39 characters maximum. A whitelist, like the X handle rule, so a
# value that reaches a URL cannot carry path or query syntax.
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

PROFILES_ENV = "CORPUS_PROFILES"
PROFILES_FILENAME = "profiles.yaml"


class IdentityError(ValueError):
    """A malformed identity card, an unknown anchor kind, or an unusable file."""


def slugify(text: str) -> str:
    """ "Jane Smith" -> "janesmith". Used for target keys and name affinity."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _host_of(url: str) -> str:
    parsed = urlparse(url if "//" in url else f"https://{url}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def normalize_anchor(kind: str, value: str) -> str:
    """Canonical form of one anchor, or raise.

    Validation happens here rather than at the point of use because an anchor
    is interpolated into URLs by several callers, and each of them getting it
    right independently is how one of them gets it wrong.
    """
    kind = kind.strip().lower()
    raw = (value or "").strip()
    if not raw:
        raise IdentityError(f"anchor {kind!r} is empty")
    if kind in REFUSED_ANCHORS:
        raise IdentityError(f"{kind} is not a supported anchor: {REFUSED_ANCHORS[kind]}")
    if kind not in KNOWN_ANCHORS:
        raise IdentityError(
            f"unknown anchor {kind!r}. Valid anchors: {', '.join(KNOWN_ANCHORS)}. "
            "An anchor the tool does not know how to follow would be silently ignored."
        )

    if kind == "x":
        try:
            return validate_handle(raw)
        except InvalidHandle as exc:
            raise IdentityError(f"x anchor: {exc}") from exc

    if kind == "github":
        login = raw.strip("/")
        if "github.com" in login:
            login = login.split("github.com/", 1)[-1].split("/")[0]
        login = login.lstrip("@")
        if not _GITHUB_LOGIN.match(login):
            raise IdentityError(
                f"github anchor {value!r} is not a GitHub username. Expected up to 39 "
                "alphanumerics with single internal hyphens, e.g. `jsmith`."
            )
        return login

    if kind == "substack":
        domain = raw.removeprefix("https://").removeprefix("http://").strip("/")
        if not domain or "/" in domain or "." not in domain:
            raise IdentityError(
                f"substack anchor {value!r} should be a domain, e.g. `janesmith.substack.com`"
            )
        return domain.lower()

    # site, rss
    url = raw if "//" in raw else f"https://{raw}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise IdentityError(f"{kind} anchor {value!r} is not an http(s) URL")
    return url.rstrip("/") if kind == "site" else url


@dataclass
class IdentityCard:
    """Who we are reading, and what is confirmed to be theirs."""

    key: str
    name: str = ""
    employer: str = ""
    role: str = ""
    location: str = ""
    anchors: dict[str, str] = field(default_factory=dict)
    #: Known false positives — a URL here is never ingested, whatever it scores.
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.key = slugify(self.key) or self.key
        self.anchors = {k: normalize_anchor(k, v) for k, v in self.anchors.items() if v}
        self.exclude = [e.strip() for e in self.exclude if e and e.strip()]

    # -- derived facts ----------------------------------------------------

    @property
    def slug(self) -> str:
        return slugify(self.name) if self.name else self.key

    @property
    def x_handle(self) -> str:
        return self.anchors.get("x", "")

    @property
    def display(self) -> str:
        """What the report is titled with."""
        if self.name:
            return self.name
        if self.x_handle:
            return f"@{self.x_handle}"
        return self.key

    def anchor_hosts(self) -> set[str]:
        """Every host we already know belongs to them."""
        hosts: set[str] = set()
        for kind, value in self.anchors.items():
            if kind in ("site", "rss", "substack"):
                hosts.add(_host_of(value))
        return {h for h in hosts if h}

    def name_keys(self) -> set[str]:
        """Slugs a self-owned host or path is likely to contain.

        Both the full slug and the surname alone: `janesmith.com` and
        `smith.io` are both plausible, and a first name on its own is far too
        common to be evidence of anything.
        """
        keys = {self.slug} if self.slug else set()
        parts = [slugify(p) for p in self.name.split() if len(p) > 2]
        if len(parts) > 1:
            keys.add(parts[-1])
            keys.add(f"{parts[0][:1]}{parts[-1]}")  # jsmith
        keys.update(v for k, v in self.anchors.items() if k in ("x", "github"))
        return {k for k in keys if len(k) >= 4}

    def is_excluded(self, url: str) -> bool:
        """Did the user already tell us this one is somebody else?

        Matched on host+path prefix rather than string equality, so excluding a
        profile URL also excludes its sub-pages — which is what someone means
        when they paste one in.
        """
        target = url.rstrip("/").lower()
        for entry in self.exclude:
            candidate = entry.rstrip("/").lower()
            if target == candidate or target.startswith(candidate + "/"):
                return True
            if _host_of(entry) and _host_of(entry) == _host_of(url) and "/" not in candidate:
                return True
        return False

    # -- serialization ----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name}
        for label in ("employer", "role", "location"):
            value = getattr(self, label)
            if value:
                payload[label] = value
        if self.anchors:
            payload["anchors"] = dict(self.anchors)
        if self.exclude:
            payload["exclude"] = list(self.exclude)
        return payload

    @classmethod
    def from_dict(cls, key: str, payload: Any) -> IdentityCard:
        if not isinstance(payload, dict):
            raise IdentityError(f"target {key!r} must be a mapping, not {type(payload).__name__}")
        anchors = payload.get("anchors") or {}
        if not isinstance(anchors, dict):
            raise IdentityError(f"target {key!r}: `anchors` must be a mapping of kind -> value")
        exclude = payload.get("exclude") or []
        if isinstance(exclude, str):
            exclude = [exclude]
        if not isinstance(exclude, list):
            raise IdentityError(f"target {key!r}: `exclude` must be a list of URLs")
        return cls(
            key=key,
            name=str(payload.get("name") or "").strip(),
            employer=str(payload.get("employer") or "").strip(),
            role=str(payload.get("role") or "").strip(),
            location=str(payload.get("location") or "").strip(),
            anchors={str(k): str(v) for k, v in anchors.items() if v},
            exclude=[str(e) for e in exclude],
        )


# --------------------------------------------------------------------------
# the targets file
# --------------------------------------------------------------------------


def default_profiles_path() -> Path:
    """Where targets are read from and written to.

    A file in the working directory wins over the home-directory one, so a
    project can keep its own set of targets next to its notes.
    """
    env = os.environ.get(PROFILES_ENV)
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / PROFILES_FILENAME
    if local.exists():
        return local
    return Path.home() / ".corpus" / PROFILES_FILENAME


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise IdentityError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise IdentityError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise IdentityError(f"{path} must be a mapping at the top level")
    return raw


def load_targets(path: Path | None = None) -> dict[str, IdentityCard]:
    """Every target in the profiles file, in file order. Missing file: empty."""
    path = path or default_profiles_path()
    raw = _read_yaml(path)
    targets = raw.get("targets") or {}
    if not isinstance(targets, dict):
        raise IdentityError(f"{path}: `targets` must be a mapping of key -> card")
    return {str(key): IdentityCard.from_dict(str(key), value) for key, value in targets.items()}


def load_target(key: str, path: Path | None = None) -> IdentityCard:
    """One target, or an error naming the ones that do exist."""
    path = path or default_profiles_path()
    targets = load_targets(path)
    card = targets.get(key) or targets.get(slugify(key))
    if card is None:
        known = ", ".join(targets) or "none defined yet"
        raise IdentityError(
            f"no target {key!r} in {path}. Known targets: {known}. "
            f"Create one with `corpus profile --name ... --github ...`."
        )
    return card


def save_target(card: IdentityCard, path: Path | None = None) -> Path:
    """Write one target into the profiles file, preserving everything else.

    Read-modify-write rather than append: the file is hand-edited, and an
    appender would happily produce two `targets:` keys, of which YAML keeps
    one.
    """
    path = path or default_profiles_path()
    raw = _read_yaml(path)
    targets = raw.get("targets")
    if targets is None:
        targets = {}
    if not isinstance(targets, dict):
        raise IdentityError(f"{path}: `targets` must be a mapping of key -> card")
    targets[card.key] = card.as_dict()
    raw["targets"] = targets
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# corpus targets. Each entry is one person: who they are, and the URLs\n"
        "# and handles confirmed to be theirs. Everything discovered is scored\n"
        "# against this card, so an anchor here is the strongest evidence the\n"
        "# tool has. `exclude` holds known false positives.\n"
        "#\n"
        "# Written by `corpus profile`; safe to edit by hand.\n\n"
    )
    path.write_text(header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), "utf-8")
    return path


def build_card(
    *,
    key: str = "",
    name: str = "",
    employer: str = "",
    role: str = "",
    location: str = "",
    x: str = "",
    github: str = "",
    site: str = "",
    substack: str = "",
    rss: str = "",
    exclude: list[str] | None = None,
) -> IdentityCard:
    """An ad-hoc card from CLI flags, for a run with no saved target."""
    anchors = {"x": x, "github": github, "site": site, "substack": substack, "rss": rss}
    resolved = key or slugify(name) or x or github or slugify(_host_of(site or substack or rss))
    if not resolved:
        raise IdentityError(
            "nothing identifies this person. Give at least one of --name, --x, "
            "--github, --site, --substack, or --rss, or name a saved target with --target."
        )
    return IdentityCard(
        key=resolved,
        name=name,
        employer=employer,
        role=role,
        location=location,
        anchors={k: v for k, v in anchors.items() if v},
        exclude=list(exclude or []),
    )


def merge_flags(card: IdentityCard, **flags: str) -> IdentityCard:
    """Overlay CLI flags onto a saved card. A flag always wins.

    Returns a new card rather than mutating, so a `--target` run cannot write
    back to profiles.yaml as a side effect of an override on the command line.
    """
    anchors = dict(card.anchors)
    for kind in KNOWN_ANCHORS:
        value = (flags.get(kind) or "").strip()
        if value:
            anchors[kind] = value
    return IdentityCard(
        key=card.key,
        name=(flags.get("name") or card.name).strip(),
        employer=(flags.get("employer") or card.employer).strip(),
        role=(flags.get("role") or card.role).strip(),
        location=(flags.get("location") or card.location).strip(),
        anchors=anchors,
        exclude=list(card.exclude),
    )
