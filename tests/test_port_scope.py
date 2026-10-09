"""Which address a port is really published on, in one place.

The ports table shows a scope per row so you can see what is open to
the network without opening the Change dialog for every port. That label
is only worth anything if it agrees with the `-p` podman is actually
handed, and the two were written out separately for a while.

These tests pin the precedence (per-port bind, then network bind, then
loopback) and -- the part that matters -- that spec.effective_bind and
podman.run_argv never disagree, including the case where a port has no
bind of its own and silently inherits a network-wide exposure.
"""

from __future__ import annotations

import pytest

from vmhub import podman, spec

COMBOS = [
    # (port.bind, network.bind)
    ("", ""),                # nothing set anywhere
    ("", "127.0.0.1"),       # the default network bind
    ("", "0.0.0.0"),         # inherits a network-wide exposure
    ("0.0.0.0", "127.0.0.1"),  # per-port exposure beats a local network bind
    ("127.0.0.1", "0.0.0.0"),  # and a local port stays local regardless
    ("*", "127.0.0.1"),      # podman's own spelling, passed through as-is
]


@pytest.mark.parametrize(("port_bind", "network_bind"), COMBOS)
def test_a_per_port_bind_beats_the_network_one(
    port_bind: str, network_bind: str
) -> None:
    port = spec.Port(guest=80, host=8006, bind=port_bind)
    resolved = spec.effective_bind(port, network_bind)
    if port_bind:
        assert resolved == port_bind, resolved


@pytest.mark.parametrize(("port_bind", "network_bind"), COMBOS)
def test_an_empty_port_bind_inherits_the_network_one(
    port_bind: str, network_bind: str
) -> None:
    port = spec.Port(guest=80, host=8006, bind=port_bind)
    resolved = spec.effective_bind(port, network_bind)
    if not port_bind and network_bind:
        assert resolved == network_bind, resolved


def test_nothing_set_anywhere_falls_back_to_loopback() -> None:
    """Not 0.0.0.0. A VM with no bind written into it at all is local,
    and defaulting the other way would expose every existing VM the
    moment this code ran."""
    assert spec.effective_bind(spec.Port(guest=80, host=8006), "") == "127.0.0.1"


@pytest.mark.parametrize(("port_bind", "network_bind"), COMBOS)
def test_the_helpers_and_podman_never_disagree(
    port_bind: str, network_bind: str
) -> None:
    """The same expression feeds the label in the table and the -p on
    the command line. If they ever fork, the table starts lying about
    what is reachable from the LAN, which is the one thing it is for."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.resources.kvm = False
    vm_spec.network.bind = network_bind
    port = spec.Port(guest=443, host=8443, bind=port_bind)
    vm_spec.network.ports = [port]

    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    published = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert published == [
        f"{spec.effective_bind(port, network_bind)}:8443:443"
    ], published


def test_an_inherited_exposure_is_really_exposed_on_the_command_line() -> None:
    """The case a naive per-port reading gets wrong: the port says
    nothing, but network.bind=0.0.0.0, so podman publishes it to the
    LAN. A table that only looked at port.bind would call it local."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.resources.kvm = False
    vm_spec.network.bind = "0.0.0.0"
    port = spec.Port(guest=22, host=2222, bind="")
    vm_spec.network.ports = [port]

    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    assert "0.0.0.0:2222:22" in argv
    assert "127.0.0.1:2222:22" not in argv


def test_the_spec_hash_moves_when_the_scope_moves() -> None:
    """spec_hash is what tells a running container it is stale. A port
    that changed from local to exposed has to change the hash, or the
    old container keeps the old binding and nothing notices."""
    def hashed(network_bind: str) -> str:
        vm_spec = spec.VmSpec(name="demo")
        vm_spec.resources.kvm = False
        vm_spec.network.bind = network_bind
        vm_spec.network.ports = [spec.Port(guest=22, host=2222, bind="")]
        return podman.spec_hash(vm_spec, "uuid", "mac", None)

    assert hashed("127.0.0.1") != hashed("0.0.0.0")