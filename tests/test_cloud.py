"""Cloud images, cloud-init seeds, serial, and the doors between them.

The failure these guard against is quiet: a cloud image attached as BOOT
media boots, but lives outside data.qcow2, so every snapshot and clone
operates on the wrong drive. The tests below pin the conversion into the
VM's own disk, the CIDATA label, and the one-second-CD-ROM rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmhub import cli, cloudinit, images, iso, paths, seed, spec
from vmhub import registry
from vmhub.cli import vms
from vmhub.errors import ImageError, SpecError


def test_seed_slot_is_the_containers_second_cdrom() -> None:
    assert iso.SLOTS["seed"] == "/mount.iso"
    assert iso.SLOTS["drivers"] == "/drivers.iso"
    assert iso.SLOTS["install"] == "/start.iso"


def test_media_seed_round_trips_through_vm_toml() -> None:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.media.seed = "/tmp/seed.iso"
    back = spec.spec_from_dict(vm_spec.to_dict(), name="demo")
    assert back.media.seed == "/tmp/seed.iso"
    assert back.media.media_path("seed") == "/tmp/seed.iso"


def test_ssh_user_round_trips_and_wins_over_the_blueprint(
    make_vm, minimal_spec_data: dict
) -> None:
    """`provision --user alice` must not leave exec logging in as `debian`."""
    assert spec.spec_from_dict(
        spec.VmSpec(name="demo", ssh_user="alice").to_dict(), name="demo"
    ).ssh_user == "alice"

    vm = "web-01"
    make_vm(
        vm,
        meta={"uuid": "u", "mac": "m"},
        spec_data=dict(minimal_spec_data, ssh_user="alice"),
    )
    from vmhub import lifecycle

    assert lifecycle.spec_user_hint(vm) == "alice"


def test_exec_runs_over_ssh_and_returns_the_remote_exit_code(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    seen: list[list[str]] = []
    monkeypatch.setattr(vms, "_ssh_argv", lambda name, command=None, batch=False: ["ssh", name])
    monkeypatch.setattr(vms.shutil, "which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(
        vms.subprocess,
        "run",
        lambda argv, *a, **k: seen.append(argv) or SimpleNamespace(returncode=7),
    )

    rc = cli.cmd_exec(SimpleNamespace(name=vm, command=["true"]), cli.Out())

    assert rc == 7
    assert seen == [["ssh", vm]]


def test_seed_and_drivers_cannot_share_the_one_second_cdrom() -> None:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.media.seed = "/tmp/seed.iso"
    vm_spec.media.drivers = "/tmp/virtio.iso"
    with pytest.raises(SpecError, match="second CD-ROM"):
        vm_spec.validate()


def test_serial_is_off_by_default_and_leaves_the_container_alone() -> None:
    env = spec.VmSpec(name="demo").env(uuid="u", mac="m")
    assert "SERIAL" not in env


def test_serial_env_is_a_host_visible_socket() -> None:
    vm_spec = spec.VmSpec(name="demo")
    vm_spec.features.serial = True
    env = vm_spec.env(uuid="u", mac="m")
    assert env["SERIAL"] == f"/storage/{paths.SERIAL_SOCKET}"
    assert paths.serial_path("demo").name == paths.SERIAL_SOCKET


def test_parse_sums_ignores_noise_and_binary_markers() -> None:
    digest = "a" * 128
    text = f"""
# a comment
{digest}  debian-13-nocloud-amd64.qcow2
{'b' * 128} *debian-13-genericcloud-amd64.qcow2
not-a-digest  something
"""
    sums = images.parse_sums(text)
    assert sums["debian-13-nocloud-amd64.qcow2"] == digest
    assert sums["debian-13-genericcloud-amd64.qcow2"] == "b" * 128
    assert "something" not in sums


def test_image_aliases_resolve_to_the_same_file() -> None:
    assert images.resolve("debian").filename == "debian-13-genericcloud-amd64.qcow2"
    assert images.resolve("trixie").key == "debian13"
    assert images.resolve("debian13-genericcloud").key == "debian13"


def test_unknown_image_names_the_choices() -> None:
    with pytest.raises(Exception, match="unknown cloud image"):
        images.resolve("ubuntu")


def test_fetch_reuses_a_cache_only_after_matching_the_publishers_sums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    image = images.resolve("debian13")
    (cache / image.filename).write_bytes(b"not really an image")
    monkeypatch.setattr(paths, "image_cache_dir", lambda: cache)

    digest = images.sha512_file(cache / image.filename)
    monkeypatch.setattr(images, "_download_sums", lambda _image: digest)

    def no_download(*_a, **_k):
        raise AssertionError("fetch downloaded a cached, verified image")

    monkeypatch.setattr(images.urllib.request, "urlopen", no_download)

    path, got = images.fetch("debian13")
    assert path == cache / image.filename
    assert got == digest


def test_fetch_does_not_trust_a_cache_that_contradicts_the_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover file is not evidence that it is the right file."""
    cache = tmp_path / "cache"
    cache.mkdir()
    image = images.resolve("debian13")
    (cache / image.filename).write_bytes(b"truncated download")
    monkeypatch.setattr(paths, "image_cache_dir", lambda: cache)
    monkeypatch.setattr(images, "_download_sums", lambda _image: "0" * 128)

    def offline(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr(images.urllib.request, "urlopen", offline)

    with pytest.raises(ImageError, match="downloading"):
        images.fetch("debian13")
    assert not (cache / image.filename).exists()


def test_install_converts_into_the_vms_own_disk_and_resizes(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    paths.disk_dir(vm).mkdir(parents=True, exist_ok=True)
    source = Path("/tmp/debian.qcow2")
    source.write_bytes(b"source")

    calls: list[list[str]] = []

    def fake_run(args: list[str], *, timeout=None, check=True):
        calls.append(list(args))
        if args and args[0] == "convert":
            Path(args[-1]).write_bytes(b"converted")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(images.disk, "run", fake_run)
    record = images.install(vm, source, key="debian13", digest="d" * 128)

    assert calls[0][0] == "convert"
    assert calls[0][-1].endswith("data.qcow2.incoming")
    assert calls[1][0] == "resize"
    assert paths.disk_path(vm).read_bytes() == b"converted"
    assert record["key"] == "debian13"
    assert record["sha512"] == "d" * 128
    assert json.loads((paths.vm_dir(vm) / "image.json").read_text())["image"] == "debian.qcow2"


def test_a_failed_convert_leaves_the_old_disk_alone(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    original = paths.disk_path(vm)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"the disk that was there")

    def fail(args: list[str], *, timeout=None, check=True):
        raise ImageError("convert exploded")

    monkeypatch.setattr(images.disk, "run", fail)
    with pytest.raises(ImageError):
        images.install(vm, Path("/tmp/debian.qcow2"))
    assert original.read_bytes() == b"the disk that was there"


def test_default_user_data_refuses_to_make_a_vm_with_no_way_in() -> None:
    with pytest.raises(ImageError, match="SSH public key"):
        seed.default_user_data(hostname="web-01", ssh_keys=[])


def test_default_user_data_is_the_minimal_bootstrap() -> None:
    text = seed.default_user_data(
        hostname="web-01", ssh_keys=["ssh-ed25519 AAAA test@host"]
    )
    assert text.startswith("#cloud-config")
    assert "name: debian" in text
    assert "ssh-ed25519 AAAA test@host" in text
    assert "qemu-guest-agent" in text
    # The desktop belongs over SSH, where a failure has an exit code.
    assert "xfce" not in text.lower()


def test_a_user_name_cannot_smuggle_extra_cloud_config() -> None:
    with pytest.raises(ImageError, match="invalid guest user name"):
        seed.default_user_data(
            hostname="web-01",
            user="debian\nruncmd: [touch /owned]",
            ssh_keys=["ssh-ed25519 AAAA k"],
        )


def test_build_writes_the_three_files_and_a_cidata_iso(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    text = seed.default_user_data(hostname=vm, ssh_keys=["ssh-ed25519 AAAA k"])

    def fake_build(seed_dir: Path, out: Path, **_: object) -> None:
        out.write_bytes(b"iso")

    monkeypatch.setattr(seed, "_build_iso", fake_build)
    monkeypatch.setattr(
        seed.iso,
        "validate",
        lambda path: iso.IsoInfo(str(path), path.name, 4, "CIDATA", True, True),
    )

    out = seed.build(vm, user_data=text, instance_id="web-01-1")
    assert out == paths.seed_dir(vm) / paths.SEED_ISO
    assert (paths.seed_dir(vm) / "user-data").read_text().startswith("#cloud-config")
    assert "instance-id: web-01-1" in (paths.seed_dir(vm) / "meta-data").read_text()
    assert "dhcp4: true" in (paths.seed_dir(vm) / "network-config").read_text()


def test_build_refuses_a_seed_cloud_init_will_never_find(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    monkeypatch.setattr(seed, "_build_iso", lambda seed_dir, out, **_: out.write_bytes(b"x"))
    monkeypatch.setattr(
        seed.iso,
        "validate",
        lambda path: iso.IsoInfo(str(path), path.name, 4, "WRONG", True, True),
    )
    with pytest.raises(ImageError, match="CIDATA"):
        seed.build(vm, user_data="#cloud-config\n", instance_id="x")


class _FakeQga:
    def __init__(self, path: Path, *, timeout: float = 6.0) -> None:
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, command: str, **arguments):
        if command == "guest-ping":
            return {}
        if command == "guest-exec":
            return {"pid": 7}
        if command == "guest-exec-status":
            return {"exited": True, "exitcode": 0}
        raise AssertionError(command)


def test_wait_finishes_when_cloud_init_wrote_boot_finished(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=minimal_spec_data)
    monkeypatch.setattr(cloudinit.qga, "Qga", _FakeQga)
    assert cloudinit.wait(vm, timeout=1.0, progress=lambda _m: None) is True


def test_attach_seed_displaces_a_drivers_iso(
    make_vm, minimal_spec_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = "web-01"
    data = dict(minimal_spec_data)
    data["media"] = {"drivers": "/tmp/virtio-win.iso"}
    make_vm(vm, meta={"uuid": "u", "mac": "m"}, spec_data=data)
    monkeypatch.setattr(
        iso,
        "validate",
        lambda path: iso.IsoInfo(str(path), path.name, 4, "CIDATA", True, True),
    )
    from vmhub import lifecycle

    lifecycle.attach_iso(vm, iso.SEED_SLOT, Path("/tmp/seed.iso"), progress=lambda _m: None)
    saved = registry.load_spec(vm)
    assert saved.media.seed == "/tmp/seed.iso"
    assert saved.media.drivers == ""
