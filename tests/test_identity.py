"""The identity card, and the attribution field it exists to justify.

The card is the only thing standing between "found by searching a name" and
"attributed to this person", so its failure modes are the ones worth pinning:
an anchor that is silently dropped, an anchor kind the tool cannot follow, and
a saved target that loses the rest of the file when it is written back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from corpus.identity import (
    IdentityCard,
    IdentityError,
    build_card,
    load_target,
    load_targets,
    merge_flags,
    normalize_anchor,
    save_target,
    slugify,
)
from corpus.models import ATTRIBUTION_CONFIDENCE, Document, Thread
from corpus.sources.base import attribute, make_document


def _card(**kwargs) -> IdentityCard:
    base = {
        "key": "jane",
        "name": "Jane Smith",
        "employer": "Acme Corp",
        "role": "VP Engineering",
        "anchors": {"github": "jsmith", "site": "https://janesmith.com"},
    }
    base.update(kwargs)
    return IdentityCard(**base)


# -- anchors ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "given", "expected"),
    [
        ("x", "@janesmith", "janesmith"),
        ("github", "https://github.com/jsmith", "jsmith"),
        ("github", "@jsmith", "jsmith"),
        ("site", "janesmith.com", "https://janesmith.com"),
        ("site", "https://janesmith.com/", "https://janesmith.com"),
        ("substack", "https://janesmith.substack.com/", "janesmith.substack.com"),
        ("rss", "https://janesmith.com/feed", "https://janesmith.com/feed"),
    ],
)
def test_anchors_are_normalized_to_one_form(kind: str, given: str, expected: str) -> None:
    assert normalize_anchor(kind, given) == expected


def test_an_unknown_anchor_kind_is_an_error_naming_the_valid_ones() -> None:
    """Same rule as an unknown axis: a typo that drops an anchor produces a
    report that looks complete and is not."""
    with pytest.raises(IdentityError) as exc:
        normalize_anchor("mastodon", "@jane@social.example")
    assert "mastodon" in str(exc.value)
    assert "github" in str(exc.value)


@pytest.mark.parametrize("kind", ["linkedin", "facebook", "instagram"])
def test_platforms_we_refuse_are_refused_when_the_anchor_is_written(kind: str) -> None:
    """The refusal belongs here, not three phases later mid-run."""
    with pytest.raises(IdentityError) as exc:
        normalize_anchor(kind, "https://example.com/whatever")
    assert kind in str(exc.value).lower()


def test_an_x_anchor_goes_through_the_same_validation_as_the_flag() -> None:
    with pytest.raises(IdentityError):
        normalize_anchor("x", "jane OR from:victim")


def test_a_github_anchor_cannot_carry_path_syntax() -> None:
    with pytest.raises(IdentityError):
        normalize_anchor("github", "jsmith/../../etc")


# -- the card ---------------------------------------------------------------


def test_name_keys_hold_the_forms_a_self_owned_host_actually_takes() -> None:
    keys = _card().name_keys()
    assert "janesmith" in keys  # janesmith.com
    assert "smith" in keys  # smith.io
    assert "jsmith" in keys  # the github handle, and jsmith.dev
    assert "jane" not in keys, "a bare first name is far too common to be evidence"


def test_excluding_a_url_also_excludes_its_subpages() -> None:
    card = _card(exclude=["https://linkedin.com/in/jane-smith-attorney"])
    assert card.is_excluded("https://linkedin.com/in/jane-smith-attorney")
    assert card.is_excluded("https://linkedin.com/in/jane-smith-attorney/recent-activity")
    assert not card.is_excluded("https://linkedin.com/in/jane-smith-engineer")


def test_excluding_a_bare_host_excludes_the_whole_host() -> None:
    card = _card(exclude=["othersmith.com"])
    assert card.is_excluded("https://othersmith.com/blog/post")


def test_the_display_name_prefers_the_real_name_over_the_handle() -> None:
    assert _card().display == "Jane Smith"
    assert IdentityCard(key="pg", anchors={"x": "paulg"}).display == "@paulg"
    assert IdentityCard(key="nobody").display == "nobody"


def test_a_card_needs_something_to_identify_it() -> None:
    with pytest.raises(IdentityError) as exc:
        build_card()
    assert "--target" in str(exc.value)


def test_a_card_built_from_flags_keys_itself_off_whatever_it_has() -> None:
    assert build_card(name="Jane Smith").key == "janesmith"
    assert build_card(x="paulg").key == "paulg"
    assert build_card(github="jsmith").key == "jsmith"
    assert build_card(site="https://blog.example.com").key == "blogexamplecom"


def test_flags_override_a_saved_card_without_writing_back(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    save_target(_card(), path)
    card = load_target("jane", path)

    merged = merge_flags(card, x="janesmith", employer="Beta Inc")
    assert merged.anchors["x"] == "janesmith"
    assert merged.anchors["github"] == "jsmith", "an override must not drop the other anchors"
    assert merged.employer == "Beta Inc"

    # The file is untouched: an override on the command line is not a decision
    # to edit the user's notes.
    assert load_target("jane", path).employer == "Acme Corp"
    assert "x" not in load_target("jane", path).anchors


# -- the profiles file ------------------------------------------------------


def test_a_saved_target_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    save_target(_card(exclude=["https://linkedin.com/in/other"]), path)
    card = load_target("jane", path)
    assert card.name == "Jane Smith"
    assert card.employer == "Acme Corp"
    assert card.role == "VP Engineering"
    assert card.anchors == {"github": "jsmith", "site": "https://janesmith.com"}
    assert card.exclude == ["https://linkedin.com/in/other"]


def test_saving_a_target_preserves_the_rest_of_the_file(tmp_path: Path) -> None:
    """The file is hand-edited. A writer that appends produces two `targets:`
    keys, of which YAML keeps exactly one."""
    path = tmp_path / "profiles.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "targets": {"bob": {"name": "Bob Jones", "anchors": {"x": "bobjones"}}},
                "notes": "keep me",
            }
        ),
        encoding="utf-8",
    )
    save_target(_card(), path)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["notes"] == "keep me"
    assert set(raw["targets"]) == {"bob", "jane"}
    assert load_target("bob", path).name == "Bob Jones"


def test_an_unknown_target_names_the_ones_that_exist(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    save_target(_card(), path)
    with pytest.raises(IdentityError) as exc:
        load_target("janet", path)
    assert "jane" in str(exc.value)


def test_a_missing_profiles_file_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    assert load_targets(tmp_path / "nothing-here.yaml") == {}


def test_a_malformed_target_is_an_error_not_a_silent_empty_card(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text("targets:\n  jane: 'just a string'\n", encoding="utf-8")
    with pytest.raises(IdentityError):
        load_targets(path)


def test_the_profiles_path_follows_the_environment(monkeypatch, tmp_path: Path) -> None:
    from corpus.identity import default_profiles_path

    monkeypatch.setenv("CORPUS_PROFILES", str(tmp_path / "elsewhere.yaml"))
    assert default_profiles_path() == tmp_path / "elsewhere.yaml"


def test_slugify_is_stable_across_the_forms_a_name_arrives_in() -> None:
    assert slugify("Jane Smith") == slugify("jane  smith") == "janesmith"
    assert slugify("Jean-Luc O'Brien") == "jeanlucobrien"


# -- attribution on the document --------------------------------------------


def test_a_document_defaults_to_anchor_so_old_corpora_load_honestly() -> None:
    """Every document written before discovery existed came from a handle the
    user typed. That is the certain case, and the default must say so."""
    doc = Document(
        source="x",
        source_id="1",
        url="https://x.com/a/status/1",
        author_handle="a",
        published_at="2024-01-01T00:00:00Z",
        kind="original",
        body="hello",
    )
    assert doc.attribution == "anchor"
    assert doc.attribution_confidence == 1.0


def test_make_document_takes_its_confidence_from_the_tier() -> None:
    doc = make_document(
        source="rss",
        source_id="1",
        url="https://janesmith.com/p",
        author_handle="jane",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        title="T",
        body="B",
        links=[],
        raw={},
        attribution="linked",
        attribution_basis="linked from github.com/jsmith bio",
    )
    assert doc.attribution_confidence == ATTRIBUTION_CONFIDENCE["linked"]
    assert "github.com/jsmith" in doc.attribution_basis


def test_attribute_stamps_a_whole_batch_the_caller_fetched() -> None:
    docs = [
        Document(
            source="rss",
            source_id=str(i),
            url=f"https://janesmith.com/{i}",
            author_handle="jane",
            published_at="2024-01-01T00:00:00Z",
            kind="original",
            body="x",
        )
        for i in range(3)
    ]
    attribute(docs, attribution="corroborated", basis="employer named on the page")
    assert {d.attribution for d in docs} == {"corroborated"}
    assert all(d.attribution_confidence == ATTRIBUTION_CONFIDENCE["corroborated"] for d in docs)


def test_a_collapsed_thread_keeps_the_roots_attribution() -> None:
    """Threads are the best documents in an X corpus. Losing their attribution
    in the collapse would silently downgrade them to the field default."""
    parts = [
        Document(
            source="x",
            source_id=str(i),
            url=f"https://x.com/a/status/{i}",
            author_handle="a",
            published_at="2024-01-01T00:00:00Z",
            kind="original" if i == 0 else "reply",
            body=f"part {i}",
            attribution="linked",
            attribution_basis="linked from the site's about page",
            attribution_confidence=0.9,
        )
        for i in range(3)
    ]
    collapsed = Thread(root_id="0", parts=parts).collapse()
    assert collapsed.attribution == "linked"
    assert collapsed.attribution_confidence == 0.9
    assert "about page" in collapsed.attribution_basis
