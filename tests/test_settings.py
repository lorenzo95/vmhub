"""The install-wide defaults, and what is not allowed into them.

These live outside the checkout, so a value here is one the person chose for
the whole install rather than something that shipped. That makes validation
worth testing on its own: a value refused now is a message and a red line,
while the same value accepted now is an opaque podman failure at the first
start of a VM created later -- on a machine that had already been configured
correctly.

The image tag is the one field with a shape. rdp_username and share_path are
free text: there is nothing to check them against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmhub import settings, spec


def test_the_image_tag_is_offered_as_a_setting() -> None:
    """The point of the exercise: the pin becomes a preference."""
    assert "image_tag" in settings.FIELDS


def test_every_setting_explains_itself() -> None:
    """`vmctl config` and settings.toml print these, and the file is meant to
    be readable by whoever opens it in an editor with no docs to hand."""
    for key, description in settings.FIELDS.items():
        assert description.strip(), f"{key} has no description"


@pytest.mark.parametrize("tag", ["", "7.50", "8.1.2", "latest"])
def test_a_plain_tag_is_accepted(tag: str, project_home: Path) -> None:
    assert settings.image_tag_problem(tag) is None
    settings.set_value("image_tag", tag)
    assert settings.get("image_tag") == tag


@pytest.mark.parametrize(
    "tag",
    [
        "7.5 0",
        "docker.io/qemux/qemu:8.0",
        "qemux/qemu",
        "8.0@sha256",
    ],
)
def test_something_that_is_not_just_a_tag_is_refused(tag: str) -> None:
    assert settings.image_tag_problem(tag) is not None


def test_the_refusal_says_what_to_type_instead() -> None:
    """A bare "invalid" names the problem and nothing else, so the person
    still does not know whether the repository or the version was wanted."""
    problem = settings.image_tag_problem("docker.io/qemux/qemu:8.0")
    assert problem is not None
    assert "image_tag" in problem, problem
    assert "7.50" in problem, problem


def test_a_refused_value_leaves_the_file_that_was_there(project_home: Path) -> None:
    """Validate before writing, not after: a rejected Save must not leave
    half the dialog's changes behind, with the tag reverted and everything
    else applied."""
    settings.set_value("image_tag", "8.1.2")
    settings.set_value("rdp_username", "someone")
    before = settings.config_path().read_text()
    with pytest.raises(settings.SettingError):
        settings.set_value("image_tag", "7.5 0")
    assert settings.config_path().read_text() == before


def test_a_hand_edited_bad_tag_degrades_to_the_shipped_pin(
    project_home: Path,
) -> None:
    """save() refuses these, but the file is a plain TOML file that anyone can
    edit. load() applies the same rule, because the alternative is handing
    "docker.io/qemux/qemu:7.5 0" to podman at the first start of a new VM."""
    settings.config_path().parent.mkdir(parents=True, exist_ok=True)
    settings.config_path().write_text('image_tag = "7.5 0"\n')
    assert settings.load()["image_tag"] == ""
    assert spec.default_tag() == spec.DEFAULT_TAG


def test_a_hand_edited_good_tag_is_honoured(project_home: Path) -> None:
    """The flip side: validation must not throw away what it accepts."""
    settings.config_path().parent.mkdir(parents=True, exist_ok=True)
    settings.config_path().write_text('image_tag = "8.1.2"\n')
    assert spec.default_tag() == "8.1.2"


def test_an_unreadable_settings_file_does_not_break_the_default(
    project_home: Path,
) -> None:
    """settings.load() degrades to nothing configured rather than raising,
    which is what keeps a corrupt file from costing someone their VM list."""
    settings.config_path().parent.mkdir(parents=True, exist_ok=True)
    settings.config_path().write_text("this is not = = toml\n")
    assert spec.default_tag() == spec.DEFAULT_TAG


def test_the_written_file_keeps_the_other_settings(project_home: Path) -> None:
    """save() rewrites the whole file from FIELDS, so a key it does not know
    about would be dropped on every Save. Adding a field must not cost the
    ones that were already there."""
    settings.set_value("rdp_username", "someone")
    settings.set_value("share_path", "/home/someone/share")
    settings.set_value("image_tag", "8.1.2")
    settings.set_value("rdp_resolution", "2560x1440")
    assert settings.load() == {
        "rdp_username": "someone",
        "share_path": "/home/someone/share",
        "image_tag": "8.1.2",
        "rdp_resolution": "2560x1440",
    }

def test_an_unset_tag_reports_the_value_it_will_use(project_home: Path) -> None:
    """"(unset)" is true and answers a question nobody asked. The question is
    what a new VM will pull, and that number has to be on the screen."""
    assert settings.fallback("image_tag") == spec.DEFAULT_TAG
    assert settings.effective("image_tag") == spec.DEFAULT_TAG


def test_a_set_tag_reports_itself(project_home: Path) -> None:
    settings.set_value("image_tag", "8.1.2")
    assert settings.fallback("image_tag") == spec.DEFAULT_TAG, "the fallback still exists"
    assert settings.effective("image_tag") == "8.1.2"


def test_a_field_with_no_default_reports_none(project_home: Path) -> None:
    """rdp_username and share_path really do end up empty, so "(unset)" is the
    honest thing to say and there is nothing better to show instead."""
    for key in ("rdp_username", "share_path"):
        assert settings.fallback(key) == "", key
        assert settings.effective(key) == "", key


def test_the_fallback_follows_the_shipped_pin_not_a_second_copy() -> None:
    """It has to be asked of spec, not stored here, or the number on screen
    would be a second source of truth that could disagree with the one used."""
    assert settings.fallback("image_tag") == spec.DEFAULT_TAG


def test_the_fallback_is_the_shipped_pin_even_once_one_is_chosen() -> None:
    """fallback() answers "what if this were unset"; effective() answers "what
    is it". Collapsing them into one function would mean a configured value
    gets reported as the default, which is the one thing the dimmed wording
    here exists to avoid."""
    settings.set_value("image_tag", "8.1.2")
    assert settings.fallback("image_tag") == spec.DEFAULT_TAG
    assert settings.effective("image_tag") == "8.1.2"
    assert settings.effective("image_tag") == spec.default_tag()


@pytest.mark.parametrize(
    "value", ["", "1920x1080", "2560x1440", "3840x2160", "1024x768"]
)
def test_a_plain_resolution_is_accepted(value: str, project_home: Path) -> None:
    assert settings.resolution_problem(value) is None


@pytest.mark.parametrize(
    "value", ["big", "1920", "1920x", "x1080", "1920xabc", "abcx1080", "0x1080", "99999x99999"]
)
def test_something_that_is_not_a_resolution_is_refused(value: str) -> None:
    assert settings.resolution_problem(value) is not None


def test_the_resolution_refusal_shows_the_shape_it_wants() -> None:
    """"invalid" names nothing; the person still does not know what to type."""
    problem = settings.resolution_problem("big")
    assert problem is not None
    assert "WIDTHxHEIGHT" in problem, problem


def test_the_resolution_fallback_is_the_size_vmhub_ships() -> None:
    assert settings.fallback("rdp_resolution") == "1920x1080"


def test_the_resolution_fallback_is_not_the_configured_size(project_home: Path) -> None:
    """Same distinction as image_tag: fallback is "what if unset", effective
    is "what is it". Reporting a chosen value as the default is the one
    misreading the dimmed wording exists to prevent."""
    settings.set_value("rdp_resolution", "2560x1440")
    assert settings.fallback("rdp_resolution") == "1920x1080"
    assert settings.effective("rdp_resolution") == "2560x1440"
