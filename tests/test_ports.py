"""Port allocation: pure decisions about which host number a guest port gets.

`port_is_free` opens a socket to a probe host, so every test that reaches an
allocator replaces it. What is left under test is the decision logic: which
candidate is chosen, which are refused, and which entries are considered to
collide with each other.
"""

from __future__ import annotations

import pytest

from vmhub import paths, ports, registry, spec


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


def test_reallocate_published_moves_a_role_off_the_source_number(
    make_vm, minimal_spec_data, always_free
):
    """A clone must not publish a host port its source already publishes.

    This is what made the gate red: clone() allocated fresh numbers for the
    three roles but left the source's on everything else, so a clone could
    collide with its source. That only ever worked because start() silently
    reassigned on the way in.
    """
    _vm_claiming(make_vm, minimal_spec_data, "source", 8006)
    vm_spec = _vm_with_ports(
        [22], [spec.Port(guest=8006, host=8006, role="web")]
    )
    out = ports.reallocate_published(vm_spec.network.ports)
    assert out[0].host != 8006, out


def test_reallocate_published_moves_a_custom_forward_off_the_source_number(
    make_vm, minimal_spec_data, always_free
):
    _vm_claiming(make_vm, minimal_spec_data, "source", 5433)
    vm_spec = _vm_with_ports(
        [5432], [spec.Port(guest=5432, host=5433, name="Postgres")]
    )
    out = ports.reallocate_published(vm_spec.network.ports)
    assert out[0].host != 5433, out
    assert out[0].name == "Postgres", out


def test_reallocate_published_leaves_an_unpublished_entry_alone(always_free):
    """host=0 means the user chose not to publish it; do not resurrect it."""
    vm_spec = _vm_with_ports(
        [22], [spec.Port(guest=8006, host=8006, role="web"), spec.Port(guest=5432, host=0)]
    )
    out = ports.reallocate_published(vm_spec.network.ports)
    assert out[1].host == 0, out


def test_conflicts_for_names_a_port_another_vm_claims(make_vm, minimal_spec_data):
    """Two VMs publishing one host port is a conflict to refuse, not resolve.

    start() used to call ensure_for(), which rewrote vm.toml with a different
    number. The user asked for a specific port and got another one, told only
    in a progress line that scrolls away.
    """
    _vm_claiming(make_vm, minimal_spec_data, "one", 8006)
    _vm_claiming(make_vm, minimal_spec_data, "two", 8006)
    problems = ports.conflicts_for("two")
    assert any("8006" in p and "one" in p for p in problems), problems


def test_conflicts_for_names_a_port_held_by_another_process(
    make_vm, minimal_spec_data, monkeypatch
):
    _vm_claiming(make_vm, minimal_spec_data, "solo", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: False)
    problems = ports.conflicts_for("solo")
    assert any("bound by another process" in p for p in problems), problems


def test_conflicts_for_is_quiet_when_every_port_is_usable(
    make_vm, minimal_spec_data, monkeypatch
):
    _vm_claiming(make_vm, minimal_spec_data, "solo", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: True)
    assert ports.conflicts_for("solo") == []


def test_conflicts_for_never_rewrites_the_spec(
    make_vm, minimal_spec_data, monkeypatch
):
    """The whole point of splitting it out: start() may refuse, not re-pick.

    If this ever writes, the guarantee that start() leaves vm.toml alone is
    gone, and the symptom comes back as "it changed my port".
    """
    _vm_claiming(make_vm, minimal_spec_data, "one", 8006)
    _vm_claiming(make_vm, minimal_spec_data, "two", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: False)
    before = paths.spec_path("two").read_text()
    ports.conflicts_for("two")
    assert paths.spec_path("two").read_text() == before


def test_conflicts_for_excludes_the_vm_being_checked(
    make_vm, minimal_spec_data, monkeypatch
):
    """A VM's own port is not a conflict with itself."""
    _vm_claiming(make_vm, minimal_spec_data, "solo", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: True)
    assert ports.conflicts_for("solo") == []


def test_every_allocation_in_production_reserves_what_is_already_taken() -> None:
    """allocate() probes the OS, and a stopped VM's published port is not
    listening -- so a call site that forgets `also_wanted` silently hands out
    a port another VM already owns. Nothing complains until both are started,
    and then the error names the port rather than the collision.

    Scanned rather than mocked because the mistake is in the call itself: no
    amount of exercising allocate() can tell a caller that forgot to ask.
    """
    import ast
    from pathlib import Path

    import vmhub.ports

    src_root = Path(vmhub.ports.__file__).parent
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, "id", ""
            )
            if name not in {"allocate", "allocate_any"}:
                continue
            if any(kw.arg == "also_wanted" for kw in node.keywords):
                continue
            offenders.append(f"{path.name}:{node.lineno} {name}()")
    assert not offenders, (
        "these allocation calls can hand out a host port another VM already "
        f"publishes: {offenders}"
    )


def test_repairing_leaves_a_running_vms_port_where_it_is(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """A running VM's ports are held by its own container.

    That is what "in use" looks like from the outside, and moving the
    number in vm.toml would leave the container publishing the old one.
    """
    _vm_claiming(make_vm, minimal_spec_data, "live", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda *_a, **_k: False)
    monkeypatch.setattr(ports.podman, "is_running", lambda vm: True)
    assert ports.ensure_for("live") == []
    assert registry.load_spec("live").network.published() == {8006: 22}


def test_repairing_moves_a_stopped_vms_port_out_of_the_way(
    make_vm, minimal_spec_data, monkeypatch
) -> None:
    """The other half: a port that is busy does get a new number."""
    _vm_claiming(make_vm, minimal_spec_data, "cold", 8006)
    monkeypatch.setattr(ports, "port_is_free", lambda port, *_a, **_k: port != 8006)
    monkeypatch.setattr(ports.podman, "is_running", lambda vm: False)
    changes = ports.ensure_for("cold")
    assert changes, "a busy port was left alone"
    assert registry.load_spec("cold").network.published() != {8006: 22}
