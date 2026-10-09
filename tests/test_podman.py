"""Unit tests for the new podman.run_argv behavior and the isolated-network
helpers.

Pure tests: no podman calls, no containers. run_argv is a builder; the
helpers are thin wrappers that the lifecycle code wires up. The interesting
behavior -- what flags end up in argv for each combination of internet and
per-port bind -- is exactly what the tests pin down.
"""

from __future__ import annotations

from vmhub import podman, spec


def _vm(name: str = "demo") -> spec.VmSpec:
    vm_spec = spec.VmSpec(name=name)
    vm_spec.resources.kvm = False  # the device /dev/kvm flag would dominate argv
    return vm_spec


def test_isolated_network_name_pairs_with_the_container() -> None:
    """The two names share the prefix so `podman network ls` lines up with
    `podman ps --filter name=vmhub-`. The "-isolated" suffix is what tells
    the helper apart from a container name.
    """
    assert podman.isolated_network_name("foo") == "vmhub-foo-isolated"
    assert podman.container_name("foo") == "vmhub-foo"
    assert podman.isolated_network_name("foo") != podman.container_name("foo")


def test_run_argv_adds_the_internal_network_when_internet_is_off() -> None:
    """Opt-in isolation: internet=False places the container on the
    VM's --internal network and drops the pasta flags that would
    otherwise bridge the host's eth0 into the container netns.
    """
    vm_spec = _vm()
    vm_spec.network.internet = False
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    assert "--network" in argv
    assert "vmhub-demo-isolated" in argv
    assert "/dev/net/tun" not in argv
    assert "NET_ADMIN" not in argv


def test_run_argv_keeps_pasta_flags_when_internet_is_on() -> None:
    """internet=True preserves today's behavior: pasta host-passthrough
    with the host's eth0 bridged in. The --network flag is absent because
    the rootless default network is what pasta attaches to.
    """
    vm_spec = _vm()
    vm_spec.network.internet = True
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    assert "--network" not in argv
    assert "/dev/net/tun" in argv
    assert "NET_ADMIN" in argv


def test_run_argv_uses_per_port_bind_when_set() -> None:
    """A per-port bind must override the global network.bind on the -p arg.
    Falling back would silently change a port's exposure after an edit.
    """
    vm_spec = _vm()
    vm_spec.network.bind = "127.0.0.1"
    vm_spec.network.ports = [
        spec.Port(guest=8006, host=8006, role="web", bind=""),  # uses network.bind
        spec.Port(guest=22, host=2222, role="ssh", bind="0.0.0.0"),  # override
    ]
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "127.0.0.1:8006:8006" in pairs
    assert "0.0.0.0:2222:22" in pairs


def test_run_argv_falls_back_to_network_bind_when_port_bind_is_empty() -> None:
    """Empty port.bind is the "use network.bind" sentinel. The forward
    has to honour the network-level setting, not pick a default of its
    own.
    """
    vm_spec = _vm()
    vm_spec.network.bind = "0.0.0.0"
    vm_spec.network.ports = [spec.Port(guest=22, host=2222, role="ssh", bind="")]
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert pairs == ["0.0.0.0:2222:22"]


def test_run_argv_defaults_bind_to_loopback_when_nothing_is_set() -> None:
    """Both bind fields empty: -p must still name an address. The default
    is the host's loopback (127.0.0.1), so a brand-new VM does not
    accidentally expose its ports on the LAN.
    """
    vm_spec = _vm()
    vm_spec.network.bind = ""
    vm_spec.network.ports = [spec.Port(guest=22, host=2222, role="ssh", bind="")]
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert pairs == ["127.0.0.1:2222:22"]


def test_run_argv_skips_unpublished_ports() -> None:
    """host=0 means "not assigned yet" -- it must not appear as a -p.
    The original code did this correctly already; the new per-port loop
    has to keep doing it.
    """
    vm_spec = _vm()
    vm_spec.network.ports = [
        spec.Port(guest=22, host=0, role="ssh"),
        spec.Port(guest=80, host=8080, name="http"),
    ]
    argv = podman.run_argv(vm_spec, "uuid", "mac", None)
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "127.0.0.1:0:22" not in pairs
    assert "127.0.0.1:8080:80" in pairs


def test_spec_hash_changes_when_internet_flips() -> None:
    """Toggling network.internet has to invalidate the container, so a
    running VM is recreated on the next start instead of silently keeping
    the old network mode.
    """
    # The default is now internet=True (qemux needs the internet
    # to download Alpine). Set it explicitly here so the test
    # is independent of the default.
    vm_spec = _vm()
    vm_spec.network.internet = False
    h_off = podman.spec_hash(vm_spec, "u", "m", None)
    vm_spec.network.internet = True
    h_on = podman.spec_hash(vm_spec, "u", "m", None)
    assert h_off != h_on


def test_spec_hash_changes_when_a_port_bind_changes() -> None:
    """A per-port bind change is a real config change, not a no-op the
    container has to be told about via recreate.
    """
    vm_spec = _vm()
    vm_spec.network.ports = [spec.Port(guest=22, host=2222, role="ssh", bind="127.0.0.1")]
    h_loop = podman.spec_hash(vm_spec, "u", "m", None)
    vm_spec.network.ports[0].bind = "0.0.0.0"
    h_lan = podman.spec_hash(vm_spec, "u", "m", None)
    assert h_loop != h_lan


def test_spec_hash_is_stable_across_runs() -> None:
    """The hash has to be a function of the spec, not a random id: two
    starts of the same VM with the same spec must produce the same
    hash, otherwise the start() recreate-detection would fire on every
    start.
    """
    vm_spec = _vm()
    vm_spec.network.ports = [spec.Port(guest=22, host=2222, role="ssh", bind="0.0.0.0")]
    h1 = podman.spec_hash(vm_spec, "u", "m", None)
    h2 = podman.spec_hash(vm_spec, "u", "m", None)
    assert h1 == h2
