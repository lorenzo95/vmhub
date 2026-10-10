"""The resolution RDP sessions ask for, and the window they are shown in.

Two numbers that are deliberately the same value. vmhub asked Remmina for
1920x1080 and left the window alone, so Remmina reused its own 640x480 default
and the desktop was scaled into a quarter of the space it had been given --
which is the whole reason both come from one setting rather than two.

These are preference-driven now, so the risk is not that the wrong number is
written: it is that a value is frozen somewhere. A module-level dict or a
dataclass default captures the answer when the process starts, and preferences
are editable from the running GUI -- so the profile would keep describing the
session the user was trying to change, and nothing would say so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmhub import rdp, settings


def _prefer(value: str) -> None:
    settings.set_value("rdp_resolution", value)


def test_an_unset_resolution_is_the_one_vmhub_ships() -> None:
    """An install with no settings file behaves exactly as it did before."""
    assert rdp.default_resolution() == "1920x1080"
    assert rdp.default_resolution() == rdp.shipped_resolution()


def test_a_chosen_resolution_reaches_the_session() -> None:
    _prefer("2560x1440")
    target = rdp.RdpTarget(host="h", port=1, user="", password="")
    assert target.resolution == "2560x1440"
    assert "resolution=2560x1440" in target.uri()


def test_a_chosen_resolution_reaches_the_freerdp_command() -> None:
    """Two places the number can end up, and they have to agree: the rdp://
    URI is what Remmina reads, /w: and /h: are what FreeRDP reads."""
    _prefer("2560x1440")
    argv = rdp.RdpTarget(host="h", port=1, user="", password="").freerdp_argv()
    assert "/w:2560" in argv
    assert "/h:1440" in argv


def test_a_chosen_resolution_reaches_the_remmina_profile() -> None:
    _prefer("2560x1440")
    fields = rdp.profile_fields()
    assert fields["resolution_width"] == 2560
    assert fields["resolution_height"] == 1440


def test_the_window_is_sized_to_the_session_and_not_to_remminas_memory(
    project_home: Path,
) -> None:
    """The original bug. window_width follows the session; sizing the window
    separately is how the desktop ended up scaled into a quarter of the space
    it was given."""
    _prefer("2560x1440")
    fields = rdp.profile_fields()
    assert fields["window_width"] == fields["resolution_width"]
    assert fields["window_height"] == fields["resolution_height"]


def test_the_resolution_is_read_per_call_not_frozen_at_import() -> None:
    """The failure this whole feature would have if it were written the obvious
    way. Saving a preference in the running GUI has to reach the next session
    without restarting the app."""
    first = rdp.profile_fields()["resolution_width"]
    _prefer("2560x1440")
    assert first == 1920
    assert rdp.profile_fields()["resolution_width"] == 2560


def test_clearing_the_preference_goes_back_to_the_shipped_size() -> None:
    _prefer("2560x1440")
    _prefer("")
    assert rdp.default_resolution() == "1920x1080"
    assert rdp.resolution_size() == (1920, 1080)


def test_an_unusable_stored_value_falls_back_rather_than_producing_a_size(
    project_home: Path,
) -> None:
    """Defence in depth. load() drops a bad value, but resolution_size() is
    also reached from a profile built on a value that has not been through
    load() -- and an int() here on "abc" would raise inside the middle of
    opening a session, which is the worst possible moment."""
    assert rdp.resolution_size("big") == (1920, 1080)
    assert rdp.resolution_size("1920x") == (1920, 1080)
    assert rdp.resolution_size("") == (1920, 1080)


def test_resolution_size_splits_a_good_value() -> None:
    assert rdp.resolution_size("2560x1440") == (2560, 1440)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("big", "no size at all"),
        ("1920", "one half of it"),
        ("1920x1080p", "a unit got typed in"),
        ("0x1080", "no such thing as a zero-pixel wide desktop"),
    ],
)
def test_a_mistyped_resolution_is_refused_at_save_time(value: str, why: str) -> None:
    """Refused where the person can see it, rather than at the first RDP
    connection minutes later with an xrdp error that names none of this."""
    assert settings.resolution_problem(value) is not None, why