"""Port allocation: pure decisions about which host number a guest port gets.

`port_is_free` opens a socket to a probe host, so every test that reaches an
allocator replaces it. What is left under test is the decision logic: which
candidate is chosen, which are refused, and which entries are considered to
collide with each other.
"""

from __future__ import annotations

import pytest

from vmhub import ports, spec


@pytest.fixture
def always_free(monkeypatch):
    """Pretend every port is bindable, so allocation is deterministic."""
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: True)


@pytest.fixture
def never_free(monkeypatch):
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: False)


def _vm_with_ports(guest_ports: list[int], entries: list[spec.Port]) -> spec.VmSpec:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.guest_ports = guest_ports
    vm_spec.network.ports = entries
    return vm_spec


def test_the_base_ports_are_the_documented_ones() -> None:
    """These three are what users actually type into their web UIs."""
    assert ports.BASES == {"web": 8006, "ssh": 2222, "rdp": 3389}


def test_allocate_returns_the_base_when_it_is_free(always_free: None) -> None:
    assert ports.allocate("web") == ports.BASES["web"]


def test_allocate_steps_past_a_port_it_was_told_also_wanted(always_free: None) -> None:
    """also_wanted is how a caller reserves a number it is about to use itself."""
    base = ports.BASES["web"]
    assert ports.allocate("web", also_wanted=[base]) == base + 1


def test_allocate_gives_up_rather_than_returning_a_busy_port(never_free: None) -> None:
    """Returning something already in use would fail later and less clearly."""
    with pytest.raises(ports.NoFreePort):
        ports.allocate("web")


def test_allocate_all_returns_one_port_per_role(always_free: None) -> None:
    allocated = ports.allocate_all()
    assert set(allocated) == set(ports.BASES)
    assert len(set(allocated.values())) == len(ports.BASES)


def test_allocate_any_moves_on_when_told_what_is_wanted(always_free: None) -> None:
    start = 19100
    assert ports.allocate_any(start=start) == start
    assert ports.allocate_any(start=start, also_wanted=[start]) == start + 1


def test_defaults_cover_the_web_console_and_each_forwarded_guest_port() -> None:
    """The web port is always published; the others follow network.guest_ports.

    Host numbers are deliberately left at 0 here: defaults describe *what*
    should be published, and the allocator fills in *where* afterwards.
    """
    entries = ports.defaults([22, 3389])
    guests = [entry.guest for entry in entries]
    assert guests.count(8006) == 1
    assert guests.count(22) == 1
    assert guests.count(3389) == 1


def test_defaults_omit_a_guest_port_that_is_not_forwarded() -> None:
    guests = [entry.guest for entry in ports.defaults([22])]
    assert 3389 not in guests


def _vm_claiming(make_vm, minimal_spec_data, name: str, host: int) -> None:
    spec_data = dict(minimal_spec_data)
    spec_data["network"] = dict(spec_data["network"])
    spec_data["network"]["ports"] = [{"guest": 22, "host": host, "role": "ssh"}]
    make_vm(name, meta={"uuid": name}, spec_data=spec_data)


def test_used_by_vms_maps_host_port_to_the_vm_using_it(make_vm, minimal_spec_data) -> None:
    """Keys are strings: the mapping comes straight out of a TOML file."""
    _vm_claiming(make_vm, minimal_spec_data, "keeper", 2222)
    used = ports.used_by_vms()
    assert used["2222"] == "keeper"


def test_used_by_vms_can_exclude_the_vm_being_edited(make_vm, minimal_spec_data) -> None:
    """Without the exclude, saving a port unchanged would collide with itself."""
    _vm_claiming(make_vm, minimal_spec_data, "self", 2222)
    assert "2222" in ports.used_by_vms()
    assert "2222" not in ports.used_by_vms(exclude="self")


def test_collisions_names_a_host_port_two_vms_both_claim(
    make_vm, minimal_spec_data
) -> None:
    _vm_claiming(make_vm, minimal_spec_data, "first", 2222)
    _vm_claiming(make_vm, minimal_spec_data, "second", 2222)

    found = ports.collisions()
    assert 2222 in found
    assert sorted(found[2222]) == ["first", "second"]


def test_no_collisions_when_the_two_vms_use_different_hosts(
    make_vm, minimal_spec_data
) -> None:
    _vm_claiming(make_vm, minimal_spec_data, "first", 2222)
    _vm_claiming(make_vm, minimal_spec_data, "second", 2223)
    assert ports.collisions() == {}


def test_host_taken_by_other_ignores_the_entry_being_edited() -> None:
    """Saving a port back onto its own number is not a collision."""
    entry = spec.Port(guest=22, host=2222, role="ssh")
    net = spec.Network(ports=[entry])
    assert not ports.host_taken_by_other(net, 2222, exclude=entry)


def test_host_taken_by_other_notices_a_different_entry() -> None:
    entry = spec.Port(guest=22, host=2222, role="ssh")
    other = spec.Port(guest=80, host=2222, name="a")
    net = spec.Network(ports=[entry, other])
    assert ports.host_taken_by_other(net, 2222, exclude=spec.Port(guest=2223, host=2222))


def test_prune_guest_forward_leaves_a_still_forwarded_port_alone() -> None:
    """The function only tidies up forwards that no longer exist."""
    net = spec.Network(
        guest_ports=[22],
        ports=[spec.Port(guest=22, host=2222, role="ssh")],
    )
    ports.prune_guest_forward(net, 22)
    assert 22 in net.guest_ports


def test_prune_guest_forward_drops_an_unforwarded_guest_port() -> None:
    """A forward also turns on the container's forwarding for that guest port,
    so taking the forward away has to take the request back with it."""
    net = spec.Network(
        guest_ports=[22, 3389],
        ports=[spec.Port(guest=22, host=2222, role="ssh")],
    )
    ports.prune_guest_forward(net, 3389)
    assert 3389 not in net.guest_ports
    assert 22 in net.guest_ports


def test_verify_warns_when_a_forward_is_not_a_guest_port() -> None:
    """Forwarding something the container was not told to forward is a trap."""
    vm_spec = _vm_with_ports(
        [22],
        [
            spec.Port(guest=22, host=2222, role="ssh"),
            spec.Port(guest=3389, host=3389, role="rdp"),
        ],
    )
    problems = ports.verify(vm_spec, check_busy=False)
    assert any("3389" in problem for problem in problems), problems


def test_verify_is_quiet_when_every_forward_is_a_guest_port() -> None:
    vm_spec = _vm_with_ports(
        [22],
        [
            spec.Port(guest=22, host=2222, role="ssh"),
            spec.Port(guest=8006, host=8006, role="web"),
        ],
    )
    assert ports.verify(vm_spec, check_busy=False) == []


def test_verify_ignores_an_unallocated_host() -> None:
    """host=0 means "not assigned yet", not "port zero is busy"."""
    vm_spec = _vm_with_ports([22], [spec.Port(guest=22, host=0, role="ssh")])
    assert ports.verify(vm_spec, check_busy=True) == []