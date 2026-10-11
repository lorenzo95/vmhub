"""Ports handed to a VM at creation have to avoid the ones already spoken for.

The reservation is easy to get wrong in a way that only shows up later.
allocate() probes the operating system, and a *stopped* VM's published port
is not listening -- so the OS calls it free, and a new VM can be given a host
port that another VM already owns. Nothing complains at creation time. Both
VMs start fine one at a time. The second one then fails to bind, at start,
with an error that names a port rather than the collision that caused it.

materialize() is exercised directly here rather than through libvirt: it takes
an already-described Domain and its own docstring says it was split out
exactly so the conversion can be run without a reachable libvirt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmhub import libvirt, ports, registry, spec


def _existing_vm(make_vm, minimal_spec_data, name: str, **roles: int) -> str:
    """A VM that already publishes the given role -> host port mappings.

    meta= matters: without it make_vm writes no meta.json, the registry does
    not see the VM at all, and used_by_vms() is empty -- which would make this
    test pass for the wrong reason.
    """
    make_vm(name, meta={"name": name, "created": "2026-01-01"}, spec_data=minimal_spec_data)
    vm_spec = registry.load_spec(name)
    for role, host in roles.items():
        vm_spec.network.set_role(role, {"web": 8006, "ssh": 22}[role], host)
    registry.save_spec(vm_spec)
    return name


def _fake_domain(source: Path, name: str = "donated") -> libvirt.Domain:
    return libvirt.Domain(
        name=name,
        state="shut off",
        memory_kib=1024 * 1024,
        vcpus=2,
        disks=[str(source)],
    )


@pytest.fixture
def donated_disk(tmp_path: Path) -> Path:
    """A small file for materialize() to convert. Sparse, so it costs nothing."""
    source = tmp_path / "donated.img"
    with source.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    return source


def test_an_import_does_not_take_a_port_another_vm_publishes(
    make_vm, minimal_spec_data, project_home, monkeypatch, donated_disk
) -> None:
    """The regression.

    Both VMs would be given web 8006, because the existing VM is stopped and
    so nothing is listening on 8006 for the OS probe to find.
    """
    _existing_vm(make_vm, minimal_spec_data, "resident", web=8006, ssh=2222)

    # Isolate from whatever else this machine has listening, so the test
    # turns on the reservation rather than on the state of the host.
    monkeypatch.setattr(ports, "port_is_free", lambda p: True)

    libvirt.materialize(_fake_domain(donated_disk), new_name="guest")

    published = registry.load_spec("guest").network.published()
    assert published.get(8006) != 8006, f"guest took web 8006: {published}"
    assert 2222 not in published.values(), f"guest took ssh 2222: {published}"


def test_an_import_takes_the_base_ports_when_nothing_is_published(
    make_vm, minimal_spec_data, project_home, monkeypatch, donated_disk
) -> None:
    """The reservation must not cost the common case its familiar ports."""
    monkeypatch.setattr(ports, "port_is_free", lambda p: True)

    libvirt.materialize(_fake_domain(donated_disk), new_name="solo")

    published = registry.load_spec("solo").network.published()
    assert published == {8006: 8006, 2222: 22}, published


def test_the_two_roles_cannot_collide_with_each_other(
    make_vm, minimal_spec_data, project_home, monkeypatch, donated_disk
) -> None:
    """Each pick is folded into the reservation before the next is made, so a
    narrow or unusual BASES table cannot hand the same port to both."""
    monkeypatch.setattr(ports, "port_is_free", lambda p: True)
    # Force the two ranges to overlap, which BASES does not do today.
    monkeypatch.setitem(ports.BASES, "ssh", ports.BASES["web"])

    libvirt.materialize(_fake_domain(donated_disk), new_name="overlap")

    hosts = list(registry.load_spec("overlap").network.published().values())
    assert len(hosts) == len(set(hosts)), f"the same host port twice: {hosts}"