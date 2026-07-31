"""Secrets hygiene, enforced by the suite rather than by remembering.

Two independent things are tested here, and both matter:

1. The tree is clean right now (`scripts/check_secrets.sh` exits 0).
2. The scanner would actually catch a leak. A scanner that always passes is
   worse than no scanner, because it buys confidence it has not earned — so
   every pattern gets a planted-leak test.

Offline: no network, no keys required.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from corpus.redact import MASK, redact, secret_values
from corpus.sources.base import SourceError
from corpus.x.providers import ProviderError, ScopeViolation

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_secrets.sh"

# Fake credentials, assembled from pieces so this file does not contain the
# literal prefixes and therefore does not trip the scanner it is testing.
FAKE_ANTHROPIC = "sk-" + "ant-" + "api03-" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"  # secrets-scan: ok
FAKE_TWITTERAPI = "new" + "1_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"  # secrets-scan: ok


def _bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    # Git for Windows ships bash but does not always put it on PATH.
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def _run_scanner() -> subprocess.CompletedProcess[str]:
    bash = _bash()
    if bash is None:  # pragma: no cover - depends on the developer's machine
        pytest.skip("bash not found; run scripts/check_secrets.sh manually")
    return subprocess.run(
        [bash, str(SCRIPT)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_tree_has_no_committable_secrets() -> None:
    result = _run_scanner()
    assert result.returncode == 0, (
        "scripts/check_secrets.sh found credential material:\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("anthropic prefix", f'KEY = "{FAKE_ANTHROPIC}"\n'),
        ("twitterapi prefix", f'KEY = "{FAKE_TWITTERAPI}"\n'),
        ("assigned env var", "X_API_KEY=Zm9vYmFyYmF6cXV1eDEyMzQ1Njc4OTA=\n"),  # secrets-scan: ok
        ("bare high entropy", 'blob = "Ax7Kq9ZmPl2WsdE4rTyU8iOp1AsdFgHj5KlZ"\n'),  # secrets-scan: ok
    ],
)
def test_scanner_catches_planted_leak(name: str, content: str) -> None:
    """Every pattern earns its place by catching something."""
    probe = REPO / f"_secretscan_probe_{name.replace(' ', '_')}.txt"
    probe.write_text(content, encoding="utf-8")
    try:
        result = _run_scanner()
    finally:
        probe.unlink(missing_ok=True)
    assert result.returncode == 1, f"scanner missed a planted {name} leak"
    assert probe.name in result.stdout


def test_env_example_holds_placeholders_only() -> None:
    """Phase 0.4: .env.example must never carry a real key."""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if not name.upper().endswith(("KEY", "TOKEN", "SECRET", "PASSWORD")):
            continue
        assert value, f"{name} has no placeholder value"
        assert any(
            marker in value.lower() for marker in ("your_", "_here", "xxx", "placeholder")
        ), f".env.example {name}={value!r} does not look like a placeholder"


def test_env_is_gitignored_and_untracked() -> None:
    """Phase 0.1, asserted rather than assumed."""
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=REPO, capture_output=True
    )
    assert ignored.returncode == 0, ".env is not gitignored"

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=REPO, capture_output=True
    )
    assert tracked.returncode != 0, ".env is tracked by git"

    history = subprocess.run(
        ["git", "log", "--all", "--full-history", "--oneline", "--", ".env"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert not history.stdout.strip(), (
        "'.env' appears in git history. The key is compromised and must be "
        f"rotated, not merely deleted:\n{history.stdout}"
    )


# --------------------------------------------------------------------------
# redact()
# --------------------------------------------------------------------------


def test_redact_masks_exact_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_API_KEY", FAKE_TWITTERAPI)
    leaked = f"401 from /twitter/user/info: {{'key': '{FAKE_TWITTERAPI}'}}"
    out = redact(leaked)
    assert FAKE_TWITTERAPI not in out
    assert MASK in out
    assert "401 from /twitter/user/info" in out  # the useful part survives


def test_redact_masks_unknown_keys_by_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that never came from this process's environment is still masked."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("X_API_KEY", raising=False)
    assert FAKE_ANTHROPIC not in redact(f"error: bad key {FAKE_ANTHROPIC}")
    assert FAKE_TWITTERAPI not in redact(f"error: bad key {FAKE_TWITTERAPI}")


@pytest.mark.parametrize(
    "leaked",
    [
        "GET https://api.twitterapi.io/twitter/tweets?apiKey=abc123XYZsecretvalue&x=1",
        "headers={'X-API-Key': 'abc123XYZsecretvalue'}",
        "Authorization: Bearer abc123XYZsecretvalue",
    ],
)
def test_redact_masks_credential_syntax(
    leaked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("X_API_KEY", raising=False)
    assert "abc123XYZsecretvalue" not in redact(leaked)


def test_redact_leaves_ordinary_text_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = "429 from /twitter/tweet/advanced_search: rate limit, retry in 30s"
    assert redact(msg) == msg


def test_redact_ignores_short_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1-char 'secret' would otherwise corrupt every message containing it."""
    monkeypatch.setenv("X_API_KEY", "x")
    assert redact("status 200, path /twitter/user/info") == (
        "status 200, path /twitter/user/info"
    )


def test_secret_values_picks_up_suffix_convention() -> None:
    env = {
        "SOME_NEW_PROVIDER_TOKEN": "a-long-enough-token-value",
        "PATH": "/usr/bin:/bin",
        "SHORT_KEY": "abc",
    }
    values = secret_values(env)
    assert "a-long-enough-token-value" in values
    assert "/usr/bin:/bin" not in values
    assert "abc" not in values  # below the minimum length


def test_redact_never_raises() -> None:
    class Exploding:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert redact(Exploding()) == "<unprintable>"


# --------------------------------------------------------------------------
# Errors redact at construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [ProviderError, ScopeViolation, SourceError])
def test_errors_redact_on_construction(
    exc_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_API_KEY", FAKE_TWITTERAPI)
    exc = exc_type(f"403 from /twitter/user/info with key {FAKE_TWITTERAPI}")
    # Not just str(): .args and repr() must be clean too, because a traceback
    # printed by the interpreter never calls str(exc).
    assert FAKE_TWITTERAPI not in str(exc)
    assert FAKE_TWITTERAPI not in repr(exc)
    assert FAKE_TWITTERAPI not in "".join(str(a) for a in exc.args)


def test_provider_error_survives_missing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction must not depend on any variable being set."""
    for name in list(os.environ):
        if name.upper().endswith(("_KEY", "_TOKEN", "_SECRET")):
            monkeypatch.delenv(name, raising=False)
    assert "boom" in str(ProviderError("boom"))
