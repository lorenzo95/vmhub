"""The spec: what survives a round trip through vm.toml, and what does not.

The interesting cases are the ones where a value equals its dataclass
default. `to_dict()` prunes anything empty, so a field set to its own
default is indistinguishable from a field nobody touched -- unless the value
is falsy-but-meaningful, which is why False and 0 are deliberately kept.
"""

from __future__ import annotations

import pytest

from vmhub import spec


def _round_trip(vm_spec: spec.VmSpec) -> spec.VmSpec:
    return spec.spec_from_dict(vm_spec.to_dict(), name=vm_spec.name)


def test_defaults_are_the_documented_ones() -> None:
    vm_spec = spec.VmSpec(name="demo")
    assert vm_spec.resources.cpus == 2
    assert vm_spec.resources.ram == "4G"
    assert vm_spec.boot.mode == "uefi"
    assert vm_spec.image.repository == "docker.io/qemux/qemu"
    assert vm_spec.network.guest_ports == [22]


def test_to_dict_drops_the_name() -> None:
    """Identity comes from the directory, so it has no business in the file."""
    assert "name" not in spec.VmSpec(name="demo").to_dict()


def test_falsy_but_meaningful_values_are_kept() -> None:
    """False and 0 match their defaults, so pruning them silently reverts them."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.features.guest_agent = False
    vm_spec.shutdown.timeout = 0
    vm_spec.display.protected = False
    vm_spec.resources.kvm = False

    data = vm_spec.to_dict()
    assert data["features"]["guest_agent"] is False
    assert data["shutdown"]["timeout"] == 0
    assert data["display"]["protected"] is False
    assert data["resources"]["kvm"] is False

    back = _round_trip(vm_spec)
    assert back.features.guest_agent is False
    assert back.shutdown.timeout == 0
    assert back.display.protected is False
    assert back.resources.kvm is False


def test_a_round_trip_preserves_every_scalar_it_can_see() -> None:
    """Nothing a user can type into vm.toml quietly turns back into a default."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.resources.cpus = 6
    vm_spec.resources.ram = "8G"
    vm_spec.resources.disk = "64G"
    vm_spec.resources.disk_type = "ide"
    vm_spec.boot.mode = "windows"
    vm_spec.image.tag = "latest"
    vm_spec.media.share_path = "/home/admin/Documents"
    vm_spec.shutdown.timeout = 45

    back = _round_trip(vm_spec)
    assert back.resources.cpus == 6
    assert back.resources.ram == "8G"
    assert back.resources.disk == "64G"
    assert back.resources.disk_type == "ide"
    assert back.boot.mode == "windows"
    assert back.image.tag == "latest"
    assert back.media.share_path == "/home/admin/Documents"
    assert back.shutdown.timeout == 45


def test_a_round_trip_preserves_the_port_table() -> None:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=8006, host=8006, role="web"),
        spec.Port(guest=22, host=2222, role="ssh"),
        spec.Port(guest=3389, host=3389, role="rdp"),
    ]
    back = _round_trip(vm_spec)
    assert len(back.network.ports) == 3
    assert back.network.published() == {8006: 8006, 2222: 22, 3389: 3389}
    assert back.network.by_role("rdp").guest == 3389
    assert back.network.host_for("web") == 8006


@pytest.mark.parametrize(
    ("value", "empty"),
    [
        (None, True),
        ("", True),
        ([], True),
        ({}, True),
        ("x", False),
        ([22], False),
        ({"a": 1}, False),
        (False, False),
        (0, False),
        (30, False),
    ],
)
def test_is_empty_truth_table(value: object, empty: bool) -> None:
    """The whole reason False and 0 are kept is visible in this one function."""
    assert spec._is_empty(value) is empty


def test_prune_keeps_a_list_whole_and_prunes_inside_it() -> None:
    """A port entry is written with only the fields it uses, but the list stays."""
    data = {
        "keep": [22, 3389],
        "ports": [{"guest": 8006, "host": 8006, "role": "web"}],
        "drop_me": {},
        "nested": {"kept": 1, "dropped": ""},
    }
    pruned = spec._prune(data)
    assert pruned["keep"] == [22, 3389]
    assert pruned["ports"] == [{"guest": 8006, "host": 8006, "role": "web"}]
    assert "drop_me" not in pruned
    assert pruned["nested"] == {"kept": 1}


def test_prune_drops_unset_fields_but_keeps_falsy_ones() -> None:
    pruned = spec._prune({"blank": "", "off": False, "zero": 0, "set": "x"})
    assert pruned == {"off": False, "zero": 0, "set": "x"}


def test_env_describes_the_machine_to_the_container() -> None:
    """The env block is the contract with the container, so name it exactly."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.resources.cpus = 6
    vm_spec.resources.ram = "8G"
    env = vm_spec.env(uuid="u", mac="m")
    assert env["CPU_CORES"] == "6"
    assert env["RAM_SIZE"] == "8G"
    assert env["DISK_TYPE"] == "scsi"


def test_env_can_switch_the_guest_agent_off() -> None:
    vm_spec = spec.VmSpec(name="demo")
    assert "QGA" in vm_spec.env(uuid="u", mac="m")
    vm_spec.features.guest_agent = False
    assert "QGA" not in vm_spec.env(uuid="u", mac="m")


def test_image_ref_composes_repository_and_tag() -> None:
    image = spec.Image(repository="docker.io/qemux/qemu", tag="latest")
    assert image.ref == "docker.io/qemux/qemu:latest"