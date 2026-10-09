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
        "Network.internet",
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
    ("network", "internet", False),
    ("network", "internet", True),
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


def test_name_problem_names_the_offending_character() -> None:
    """"invalid VM name: 'test (linked)'" alone does not say what is wrong.

    A user who typed "test (linked)" cannot see from the rule alone which
    characters to remove, and the whole message reads as a verdict on the
    clone rather than on the spelling.
    """
    problem = spec.name_problem("test (linked)")
    assert "space" not in problem or "remove" in problem, problem
    assert "remove" in problem, problem
    for char in "()":
        assert repr(char)[1:-1] in problem.replace("\"", "'"), problem


def test_name_problem_says_when_the_name_is_empty() -> None:
    """The Clone dialog used to submit exactly this."""
    assert "empty" in spec.name_problem(""), spec.name_problem("")


def test_name_problem_says_when_it_starts_wrong() -> None:
    assert "starts with" in spec.name_problem("-test"), spec.name_problem("-test")


def test_name_problem_says_when_it_is_too_long() -> None:
    assert "63" in spec.name_problem("a" * 64), spec.name_problem("a" * 64)


def test_a_valid_name_has_no_problem() -> None:
    assert spec.validate_name("test-copy") == "test-copy"
    with pytest.raises(spec.SpecError if hasattr(spec, "SpecError") else Exception):
        spec.validate_name("test copy")


def test_default_image_tag_is_pinned_not_latest() -> None:
    """The default tag must name a version.

    "latest" moves: a rebuild months from now pulls whatever the publisher
    pushed last, which is not the QEMU this release was tested against, and
    it changes under VMs that are already running.
    """
    assert spec.DEFAULT_TAG == "7.50"
    assert spec.DEFAULT_TAG != "latest"
    assert spec.DEFAULT_TAG[0].isdigit(), spec.DEFAULT_TAG
    assert "." in spec.DEFAULT_TAG, spec.DEFAULT_TAG


def test_default_image_ref_follows_the_pin() -> None:
    """Blueprints carry no [image] section, so this is what a new VM gets.

    alpine/debian13/win11/winserver2022 all omit [image] entirely and inherit
    the dataclass default. If the default drifts, every blueprint drifts.
    """
    assert spec.Image().ref == f"docker.io/qemux/qemu:{spec.DEFAULT_TAG}"


def test_explicit_latest_still_round_trips() -> None:
    """Pinning the default must not forbid asking for latest on purpose.

    The pin governs what a VM gets by accident, when nobody chose. A vm.toml
    that says latest was chosen, and rewriting it would be the pin lying
    about a decision rather than filling in an absence.
    """
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.image.tag = "latest"
    assert _round_trip(vm_spec).image.tag == "latest"


def test_a_per_port_bind_round_trips() -> None:
    """Port.bind is empty by default and survives a write-then-reload.

    Empty is the "use network.bind" signal; a non-empty override must come
    back identically. Anything that lost the bind between to_dict() and
    spec_from_dict() would silently fall back to the global bind on the
    next start.
    """
    from vmhub import spec as spec_mod

    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [
        spec.Port(guest=8006, host=8006, role="web", bind=""),
        spec.Port(guest=22, host=2222, role="ssh", bind="0.0.0.0"),
        spec.Port(guest=80, host=8080, name="http", bind="127.0.0.1"),
    ]
    back = spec_mod.spec_from_dict(vm_spec.to_dict(), name=vm_spec.name)
    binds = [p.bind for p in back.network.ports]
    assert binds == ["", "0.0.0.0", "127.0.0.1"]


def test_an_empty_port_bind_does_not_get_written() -> None:
    """The empty string is the "use network.bind" sentinel and is also the
    default, so writing it would be noise that no reader relies on.
    """
    from vmhub import spec as spec_mod

    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [spec.Port(guest=8006, host=8006, role="web", bind="")]
    data = vm_spec.to_dict()
    assert data["network"]["ports"][0].get("bind", "") == ""


def test_network_internet_default_is_on() -> None:
    """The default is internet=True: the qemux container needs the
    internet on first start to download Alpine (or whatever the
    blueprint names as boot.iso). A user who wants isolation opts
    in with network.internet=false.

    PERSIST_WHEN_FALSY still has to be on the field, so the
    chosen value round-trips through vm.toml -- otherwise a
    missing key would silently reopen the host to the guest.
    """
    from vmhub import spec as spec_mod

    assert spec.VmSpec(name="demo").network.internet is True
    data = spec.VmSpec(name="demo").to_dict()
    assert data["network"]["internet"] is True


def test_validate_rejects_a_garbage_bind() -> None:
    """A bind the kernel would reject has to fail at validate(), not at
    podman -p, which surfaces as a less helpful error three layers down.
    """
    from vmhub import spec as spec_mod

    vm_spec = spec.VmSpec(name="demo")
    vm_spec.network.ports = [spec.Port(guest=80, host=8080, name="x", bind="not-an-ip")]
    with pytest.raises(spec.SpecError):
        vm_spec.validate()


def test_validate_accepts_a_well_formed_bind() -> None:
    """127.0.0.1, 0.0.0.0 and "*" are the only binds a podman -p will take;
    they all have to clear the validator.
    """
    from vmhub import spec as spec_mod

    for bind in ("127.0.0.1", "0.0.0.0", "*"):
        vm_spec = spec.VmSpec(name="demo")
        vm_spec.network.ports = [spec.Port(guest=80, host=8080, name="x", bind=bind)]
        vm_spec.validate()  # must not raise


def test_is_bind_address_distinguishes_ipv4_from_garbage() -> None:
    from vmhub import spec as spec_mod

    assert spec_mod._is_bind_address("0.0.0.0") is True
    assert spec_mod._is_bind_address("127.0.0.1") is True
    assert spec_mod._is_bind_address("192.168.1.229") is True
    assert spec_mod._is_bind_address("*") is True
    assert spec_mod._is_bind_address("") is False
    assert spec_mod._is_bind_address("example.com") is False
    assert spec_mod._is_bind_address("::1") is False
    assert spec_mod._is_bind_address("256.0.0.1") is False
    assert spec_mod._is_bind_address("1.2.3") is False
