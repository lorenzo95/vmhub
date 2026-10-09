"""Tests for the rootless 1024 floor and the per-port expose checkbox.

The floor is the interesting one: vmhub runs as an ordinary user, so a
host port below 1024 is refused by the kernel, and vmhub used to accept
one in a spec and only find out when podman failed. Guest ports have no
such limit, and conflating the two would refuse an ordinary VM with a
web server on port 80.

Measured on the host this runs on, to fix the number in the comment
rather than guess it:
  host port 1023 -> "Failed to bind port 1023 (Permission denied)"
  host port 1024 -> accepted
  host port 9999 -> accepted, and reachable from the host
  guest port 80   -> accepted in every case, including host port 1023/1024
"""

from __future__ import annotations

import pytest

from vmhub import spec


def _vm_with_port(guest: int, host: int, **kwargs) -> spec.VmSpec:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [spec.Port(guest=guest, host=host, name="svc", **kwargs)]
    return vm_spec


# --- the floor itself -------------------------------------------------


def test_min_host_port_is_1024() -> None:
    """Not 1023, not 1025: the boundary was measured, not rounded."""
    assert spec.MIN_HOST_PORT == 1024


@pytest.mark.parametrize("host", [22, 80, 443, 1023])
def test_validate_refuses_a_host_port_below_the_floor(host: int) -> None:
    """80 is the one that would bite in practice: it is the port everyone
    reaches for, and podman refuses it outright."""
    with pytest.raises(spec.SpecError) as excinfo:
        _vm_with_port(guest=5432, host=host).validate()
    assert str(spec.MIN_HOST_PORT) in str(excinfo.value), str(excinfo.value)


def test_an_unpublished_port_is_still_exempt_from_the_floor() -> None:
    """host=0 does not mean "port zero"; it means "keep this entry, do not
    publish it", so the same guest port comes back on the next start. The
    floor guards an actual bind, and there is none to guard here."""
    _vm_with_port(guest=5432, host=0).validate()


@pytest.mark.parametrize("host", [1024, 1025, 8006, 2222, 65535])
def test_validate_accepts_a_host_port_at_or_above_the_floor(host: int) -> None:
    _vm_with_port(guest=5432, host=host).validate()


def test_the_refusal_names_the_floor_and_says_why() -> None:
    """'must be between 1 and 65535' was true and useless here. The user
    has no way to act on a range unless the message says the floor is a
    privilege thing."""
    with pytest.raises(spec.SpecError) as excinfo:
        _vm_with_port(guest=80, host=80).validate()
    message = str(excinfo.value)
    assert "1024" in message, message
    assert "normal user" in message, message


# --- guest ports are NOT restricted -----------------------------------


@pytest.mark.parametrize("guest", [1, 22, 80, 443, 3389, 1024, 65535])
def test_a_guest_port_below_the_floor_is_fine(guest: int) -> None:
    """The floor is on the host side only. A VM with a web server on 80 is
    the most ordinary VM there is; refusing it would be a regression."""
    _vm_with_port(guest=guest, host=9000).validate()


def test_a_low_guest_port_on_a_high_host_port_is_the_normal_case() -> None:
    """The shape almost every real forward has: 80 inside, 8080 outside."""
    _vm_with_port(guest=80, host=8080).validate()


def test_only_the_host_port_is_reported_not_the_guest_port() -> None:
    """The message must not send the user off to change the guest port,
    which was never the problem."""
    with pytest.raises(spec.SpecError) as excinfo:
        _vm_with_port(guest=5432, host=80).validate()
    assert "5432" not in str(excinfo.value), str(excinfo.value)


# --- the duplicate rule, now that per-port bind exists ----------------


def test_two_ports_on_one_host_number_are_refused_even_with_mixed_binds() -> None:
    """0.0.0.0 covers loopback, so a LAN-bound forward and a loopback-only
    forward cannot share a host port. Verified at the socket level:
    binding 0.0.0.0:X while 127.0.0.1:X is held fails EADDRINUSE. Before
    per-port bind this pair could not be written down at all."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=22, host=8080, name="a", bind="127.0.0.1"),
        spec.Port(guest=80, host=8080, name="b", bind="0.0.0.0"),
    ]
    with pytest.raises(spec.SpecError) as excinfo:
        vm_spec.validate()
    assert "8080" in str(excinfo.value), str(excinfo.value)


def test_two_exposed_ports_on_one_host_number_are_refused() -> None:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=22, host=8080, name="a", bind="0.0.0.0"),
        spec.Port(guest=80, host=8080, name="b", bind="0.0.0.0"),
    ]
    with pytest.raises(spec.SpecError):
        vm_spec.validate()


def test_different_host_numbers_are_fine_with_mixed_binds() -> None:
    """The rule is one host number per VM, not one bind value per VM."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=22, host=8080, name="a", bind="0.0.0.0"),
        spec.Port(guest=80, host=8081, name="b", bind=""),
    ]
    vm_spec.validate()


# --- the checkbox's two values ----------------------------------------


@pytest.mark.parametrize(
    ("bind", "exposed"),
    [
        ("", False),           # unchecked: inherit network.bind
        ("0.0.0.0", True),     # checked
        ("*", True),           # podman's spelling of the same thing
        ("127.0.0.1", False),  # explicit loopback: not exposed
    ],
)
def test_a_bind_says_whether_the_box_is_ticked(bind: str, exposed: bool) -> None:
    """* counts as exposed. A hand-written spec saying bind="*" must show
    the box ticked, not read as not-exposed and quietly lose the value on
    the next save."""
    from vmhub.ui import pane

    assert (bind in pane.EXPOSED_BINDS) is exposed, bind


def test_the_checkbox_writes_one_bind_value() -> None:
    """Checked means exactly 0.0.0.0, so podman -p gets a literal address
    rather than a spelling that has to be interpreted."""
    from vmhub.ui import pane

    assert pane.EXPOSED_BIND == "0.0.0.0"


# --- the round trip through vm.toml -----------------------------------


def test_an_exposed_port_survives_a_round_trip() -> None:
    """The checkbox has to survive to the next start. Losing bind here
    would silently turn an exposed port back into a loopback-only one."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=22, host=2222, role="ssh", bind=""),
        spec.Port(guest=8080, host=8080, name="web", bind="0.0.0.0"),
    ]
    back = spec.spec_from_dict(vm_spec.to_dict(), name="demo")
    assert [p.bind for p in back.network.ports] == ["", "0.0.0.0"]


def test_published_still_reports_host_ports_after_the_floor() -> None:
    """The floor must not disturb the port bookkeeping -- published() is
    what allocation and the duplicate check are built on."""
    vm_spec = _vm_with_port(guest=80, host=8080, bind="0.0.0.0")
    assert vm_spec.network.published() == {8080: 80}