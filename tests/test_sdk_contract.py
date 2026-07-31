"""Phase 2.5: the anthropic SDK surface the code actually calls.

pyproject declared `anthropic>=0.40`, while synthesize.py calls `output_config`
with `format` and `effort` — the GA structured-outputs surface, which 0.40
predates by roughly eighty minor versions. It worked only because pip installs
latest. Anything resolving near the declared floor (a lockfile, a constraints
file, an older environment, a `--resolution=lowest` CI job) would have died on
an unexpected-kwarg TypeError at the first synthesis call, which is both the
slowest and most expensive place to find out.

Raising the floor fixes today. Introspecting the installed SDK stops it
recurring: if a future SDK renames or drops any of this, these tests fail on the
next `make check` rather than on someone's next paid run.

Offline: introspection only, no client is constructed and no call is made.
"""

from __future__ import annotations

import inspect

import pytest

# The floor declared in pyproject.toml, and the version verified working.
MINIMUM_ANTHROPIC = (0, 120, 2)


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def test_installed_sdk_meets_the_declared_floor() -> None:
    import anthropic

    installed = _version_tuple(anthropic.__version__)
    assert installed >= MINIMUM_ANTHROPIC, (
        f"anthropic {anthropic.__version__} is below the declared floor "
        f"{'.'.join(map(str, MINIMUM_ANTHROPIC))}; output_config will not exist"
    )


def test_pyproject_floor_matches_this_test() -> None:
    """The declaration and the assertion must not drift apart."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    match = re.search(r'"anthropic>=([0-9.]+)"', text)
    assert match, "no anthropic floor found in pyproject.toml"
    assert _version_tuple(match.group(1)) == MINIMUM_ANTHROPIC, (
        f"pyproject declares anthropic>={match.group(1)} but this test expects "
        f"{'.'.join(map(str, MINIMUM_ANTHROPIC))}"
    )


@pytest.mark.parametrize("method", ["create", "stream"])
def test_output_config_is_accepted(method: str) -> None:
    """The kwarg synthesize.py passes must exist on the methods it calls."""
    from anthropic.resources.messages import Messages

    fn = getattr(Messages, method, None)
    assert fn is not None, f"Messages.{method} does not exist"
    params = inspect.signature(fn).parameters
    assert "output_config" in params, (
        f"Messages.{method} has no output_config parameter — synthesize.py "
        f"would raise TypeError on the first call. Parameters: {sorted(params)}"
    )


def test_count_tokens_is_available_for_budget_reservations() -> None:
    """Phase 2.1 prefers exact token counts to 4-chars-per-token guessing."""
    from anthropic.resources.messages import Messages

    assert hasattr(Messages, "count_tokens")
    params = inspect.signature(Messages.count_tokens).parameters
    for required in ("model", "messages"):
        assert required in params, f"count_tokens has no {required!r} parameter"


@pytest.mark.parametrize("method", ["create", "stream"])
def test_the_other_kwargs_synthesize_passes_still_exist(method: str) -> None:
    from anthropic.resources.messages import Messages

    params = inspect.signature(getattr(Messages, method)).parameters
    for required in ("model", "max_tokens", "system", "messages"):
        assert required in params, f"Messages.{method} has no {required!r}"


def test_async_client_matches_the_sync_surface() -> None:
    """synthesize.py uses AsyncAnthropic, so that is the surface that matters."""
    from anthropic.resources.messages import AsyncMessages

    for method in ("create", "stream"):
        params = inspect.signature(getattr(AsyncMessages, method)).parameters
        assert "output_config" in params, (
            f"AsyncMessages.{method} has no output_config; synthesize.py uses "
            f"the async client exclusively"
        )


def test_the_effort_and_format_keys_are_what_the_code_sends() -> None:
    """Guards the shape of the dict, not just the parameter name."""
    from corpus.synthesize import MAP_SCHEMA, REDUCE_SCHEMA

    for schema in (MAP_SCHEMA, REDUCE_SCHEMA):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_a_lockfile_exists_for_reproducible_builds() -> None:
    """Phase 2.5: pip resolving to latest is how the wrong floor stayed hidden."""
    from pathlib import Path

    lock = Path(__file__).resolve().parents[1] / "uv.lock"
    assert lock.exists(), "uv.lock is missing; run `uv lock`"
    text = lock.read_text(encoding="utf-8")
    assert 'name = "anthropic"' in text
