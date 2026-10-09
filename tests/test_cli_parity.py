"""vmctl says the same thing the ports table does.

The list used to print bind= only when a port carried a bind of its own.
A port inheriting a network-wide 0.0.0.0 was therefore listed exactly
like a loopback one -- the case you most want to notice was the one the
list stayed quiet about. These pin the wording and, more usefully, the
case that used to be silent.
"""

from __future__ import annotations

import pytest

from vmhub import cli, spec


@pytest.mark.parametrize(
    ("bind", "exposed"),
    [
        ("0.0.0.0", True),
        ("*", True),           # podman's spelling of the same thing
        ("127.0.0.1", False),
        ("::1", False),
        ("", False),
    ],
)
def test_is_exposed_reads_both_spellings_of_the_same_address(
    bind: str, exposed: bool
) -> None:
    assert spec.is_exposed(bind) is exposed, bind


def test_a_port_that_is_not_published_gets_no_scope_text() -> None:
    """host=0 means "keep the entry, do not publish it". Saying how far
    it reaches would be claiming something about a port nobody can reach."""
    assert cli.port_scope(spec.Port(guest=22, host=0), "0.0.0.0") == ""


def test_the_list_names_the_address_every_time() -> None:
    """Even a plain loopback port shows its bind, so the column means the
    same thing on every row instead of appearing only on some."""
    text = cli.port_scope(spec.Port(guest=22, host=2222, bind=""), "127.0.0.1")
    assert text == "  bind=127.0.0.1  localhost only", text


def test_an_inherited_exposure_is_called_exposed() -> None:
    """The regression. This port has no bind of its own; it inherits a
    network-wide 0.0.0.0 and podman publishes it to the LAN."""
    text = cli.port_scope(spec.Port(guest=8006, host=8006, bind=""), "0.0.0.0")
    assert text == "  bind=0.0.0.0  exposed to the network", text


def test_an_inherited_loopback_is_called_localhost() -> None:
    text = cli.port_scope(spec.Port(guest=8006, host=8006, bind=""), "127.0.0.1")
    assert text == "  bind=127.0.0.1  localhost only", text


def test_a_per_port_override_beats_the_network_in_both_directions() -> None:
    """Exposed port inside a loopback network, and a local port inside an
    exposed network. Getting the second one wrong is what made the
    previous "loopback only" summary misleading."""
    exposed = cli.port_scope(
        spec.Port(guest=8080, host=8080, bind="0.0.0.0"), "127.0.0.1"
    )
    local = cli.port_scope(
        spec.Port(guest=22, host=2222, bind="127.0.0.1"), "0.0.0.0"
    )
    assert "exposed to the network" in exposed, exposed
    assert "localhost only" in local, local


def test_a_star_bind_is_reported_verbatim_not_rewritten() -> None:
    """The spec says "*" and podman gets "*". Rewriting it in the listing
    would hide that the file holds something other than what a later
    edit compares against."""
    text = cli.port_scope(spec.Port(guest=22, host=2222, bind="*"), "127.0.0.1")
    assert text == "  bind=*  exposed to the network", text


def test_nothing_set_anywhere_reads_as_local_not_exposed() -> None:
    text = cli.port_scope(spec.Port(guest=22, host=2222, bind=""), "")
    assert "localhost only" in text, text