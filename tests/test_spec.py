"""The spec: what survives a round trip through vm.toml, and what does not.

The interesting cases are the ones where a value equals its dataclass
default. `to_dict()` prunes anything empty, so a field set to its own
default is indistinguishable from a field nobody touched -- unless the value
is falsy-but-meaningful, which is what PERSIST_WHEN_FALSY declares.
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


def test_a_field_can_declare_that_its_falsy_value_is_a_decision() -> None:
    """The declaration is what keeps these six out of the blanket rule."""
    marked = {
        f"{cls.__name__}.{f.name}"
        for cls in (
            spec.Network, spec.Resources, spec.Shutdown,
            spec.Features, spec.Image, spec.Boot,
            spec.Display, spec.Media, spec.Port,
        )
        for f in cls.__dataclass_fields__.values()
        if f.metadata.get(spec.PERSIST_WHEN_FALSY)
    }
    assert marked == {
        "Network.guest_ports",
        "Resources.kvm",
        "Resources.ram_check",
        "Shutdown.timeout",
        "Features.hv",
        "Features.guest_agent",
    }, marked


@pytest.mark.parametrize(
    ("section", "attribute", "falsy"),
    [
        ("features", "guest_agent", False),
        ("features", "hv", False),
        ("resources", "kvm", False),
        ("resources", "ram_check", False),
        ("shutdown", "timeout", 0),
        ("network", "guest_ports", []),
    ],
)
def test_a_marked_field_keeps_its_falsy_value(section: str, attribute: str, falsy) -> None:
    """Dropping the key would hand back the default, so it has to be written."""
    vm_spec = spec.VmSpec(name="demo")
    setattr(getattr(vm_spec, section), attribute, falsy)
    data = vm_spec.to_dict()
    assert section in data, f"{section} was pruned entirely: {data}"
    back = _round_trip(vm_spec)
    assert getattr(back, section).__getattribute__(attribute) == falsy


@pytest.mark.parametrize(
    ("section", "attribute", "value"),
    [
        ("display", "protected", False),
        ("boot", "iso", ""),
        ("media", "rdp_user", ""),
        ("display", "audio", False),
        ("features", "ballooning", False),
        ("shutdown", "skip_acpi", False),
    ],
)
def test_an_unmarked_field_reloads_from_its_default(section: str, attribute: str, value) -> None:
    """Not marked because the default already is that value.

    The key may be omitted, but the value the caller set must survive --
    that is the whole point: pruning an unmarked field never reverts it.
    """
    vm_spec = spec.VmSpec(name="demo")
    setattr(getattr(vm_spec, section), attribute, value)
    back = _round_trip(vm_spec)
    assert getattr(back, section).__getattribute__(attribute) == value


def test_an_unmarked_falsy_value_is_left_out_of_the_file() -> None:
    """Otherwise the declaration would document an intent and change nothing."""
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.features.ballooning = False
    vm_spec.media.rdp_user = ""
    data = vm_spec.to_dict()
    assert "ballooning" not in data.get("features", {})
    assert "rdp_user" not in data.get("media", {})


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
        (False, False),
        (0, False),
        (30, False),
        ("x", False),
        ([22], False),
        ({"a": 1}, False),
    ],
)
def test_is_empty_without_a_declared_field_drops_empty_containers_only(
    value: object, empty: bool
) -> None:
    """A bare dict has no declaration to consult, so this is the fallback rule.

    False and 0 stay: dropping them is exactly the silent revert the
    declaration exists to prevent, and a caller who never said where the value
    came from has not earned the right to have it discarded.
    """
    assert spec._is_empty(value) is empty


def test_is_empty_consults_the_declaration() -> None:
    marked = spec.Shutdown.__dataclass_fields__["timeout"]
    unmarked = spec.Boot.__dataclass_fields__["iso"]
    assert spec._is_empty(0, marked) is False
    assert spec._is_empty("", unmarked) is True
    assert spec._is_empty(None, marked) is True


def test_prune_keeps_a_list_whole_and_prunes_inside_it() -> None:
    """A port entry is written with only the fields it uses, but the list stays."""
    pruned = spec._prune(
        {
            "keep": [22, 3389],
            "ports": [{"guest": 8006, "host": 8006, "role": "web"}],
            "drop_me": {},
            "nested": {"kept": 1, "dropped": ""},
        }
    )
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