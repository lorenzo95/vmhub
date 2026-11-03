"""The fixes that make vmhub safe to run unattended.

Everything here is about a job that nobody is watching: a nightly backup, a
boot hook, a provisioning script. The shape of every bug is the same -- the
command looks like it did the thing, and reports success, and the thing it
did was not the thing the caller wanted.

The seven this file covers:

* a backup that skipped every running VM and exited 0
* a backup that was truncated and still looked like a backup
* a failed backup that left the VM down *and* out of the boot restore set
* a stop that silently cancelled the boot restore
* a systemd unit killed part-way through the restore by the default timeout
* an autostart install that reported success on a box whose VMs never return
* an autostart install made for root, because a cron job cannot say who
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmhub import autostart, cli, disk, export, lifecycle, paths, registry

from vmhub.cli.media import cmd_bundles, cmd_export_all
from vmhub.cli.vms import cmd_stop
from vmhub.errors import VmhubError
from vmhub.lifecycle.status import VmStatus


def _out(lines: list[str]) -> SimpleNamespace:
    """A stand-in for the CLI's Out that keeps what it was told."""
    return SimpleNamespace(
        step=lambda message="": lines.append(str(message)),
        say=lambda message="": lines.append(str(message)),
        warn=lambda message="": lines.append(str(message)),
        good=lambda message="": lines.append(str(message)),
        bad=lambda message="": lines.append(str(message)),
        dim=lambda message="": lines.append(str(message)),
        info=lambda message="": lines.append(str(message)),
        bold=lambda message="": lines.append(str(message)),
    )


def _make_vm(root: Path, name: str, *, running: bool = False) -> Path:
    """A VM directory the registry sees, with a real (small) disk."""
    vm_dir = root / name
    vm_dir.mkdir(parents=True, exist_ok=True)
    (vm_dir / paths.SPEC_FILE).write_text(
        '[resources]\ncpus = 2\nram = "4G"\ndisk = "1G"\n\n'
        '[network]\nmode = "user"\nbind = "127.0.0.1"\n'
    )
    (vm_dir / paths.META_FILE).write_text(
        json.dumps(
            {
                "uuid": f"uuid-{name}",
                "mac": "52:54:00:00:00:01",
                "created": 1767225600.0,
                "last_run_state": "running" if running else "stopped",
            }
        )
    )
    image = paths.disk_path(name)
    image.parent.mkdir(parents=True, exist_ok=True)
    disk.create(image, "1G")
    return vm_dir


def _fake_account(monkeypatch) -> None:
    """Pretend every account named here is a real one on this host.

    The install refuses an owner that does not exist -- a unit written for
    an account nobody has is a unit that starts nothing -- so the tests
    have to give it one that exists.
    """
    monkeypatch.setattr(
        autostart.pwd,
        "getpwnam",
        lambda n: SimpleNamespace(pw_dir=f"/home/{n}", pw_uid=1000),
    )


# --------------------------------------------------------------- C3: skipped


def test_a_backup_that_skipped_every_running_vm_is_not_a_success(
    project_home, monkeypatch
) -> None:
    """The nightly job that backed up nothing and said it had.

    `export-all` skips a running VM unless --include-running is given. The
    skip was counted in the summary line and nothing else, so the exit code
    was 0 -- and cron sees the exit code and nothing else.
    """
    _make_vm(project_home, "busy", running=True)
    monkeypatch.setattr(
        lifecycle, "list_vms", lambda: [VmStatus(name="busy", powered=True)]
    )
    exported: list[str] = []
    monkeypatch.setattr(
        export,
        "export_vm",
        lambda name, **kwargs: exported.append(name),
    )

    args = argparse.Namespace(
        names=[], dest=None, include_running=False, mode="graceful"
    )
    lines: list[str] = []
    assert cmd_export_all(args, _out(lines)) == 1
    assert exported == [], "nothing running was exported, so nothing should be called"


def test_a_backup_that_covers_everything_is_a_success(project_home, monkeypatch) -> None:
    _make_vm(project_home, "quiet")
    monkeypatch.setattr(
        lifecycle, "list_vms", lambda: [VmStatus(name="quiet", powered=False)]
    )
    monkeypatch.setattr(
        export, "export_vm", lambda name, **kwargs: Path("/tmp/quiet.vmhub.tar.gz")
    )

    args = argparse.Namespace(
        names=[], dest=None, include_running=False, mode="graceful"
    )
    assert cmd_export_all(args, _out([])) == 0


# --------------------------------------------------------------- C2: run state


def test_a_failed_backup_does_not_cancel_the_boot_restore(
    project_home, monkeypatch
) -> None:
    """One failed backup must not take the VM out of service for good.

    The backup stops the VM, exports it, and starts it again. The stop used
    to write last_run_state=stopped, so when the export failed the VM was
    down *and* no longer in the set the boot hook restores -- which is the
    same as cancelled, with nothing saying so.
    """
    _make_vm(project_home, "winni", running=True)
    monkeypatch.setattr(
        lifecycle, "list_vms", lambda: [VmStatus(name="winni", powered=True)]
    )
    stopped: list[dict] = []

    def fake_stop(name, **kwargs):
        stopped.append(kwargs)
        return "graceful"

    monkeypatch.setattr(lifecycle, "stop", fake_stop)
    # The restart is part of the cycle too, and it must not be a real one:
    # a real start would create a container on the machine running the tests.
    restarted: list[str] = []
    monkeypatch.setattr(
        lifecycle, "start", lambda name, **kwargs: restarted.append(name)
    )

    def boom(name, **kwargs):
        raise VmhubError("the disk went away")

    monkeypatch.setattr(export, "export_vm", boom)

    args = argparse.Namespace(
        names=[], dest=None, include_running=True, mode="graceful"
    )
    cmd_export_all(args, _out([]))

    assert stopped, "the VM was stopped for the backup"
    assert stopped[0].get("preserve_run_state") is True, (
        "the stop has to keep the run state, or a failed backup leaves the "
        "VM down and out of the boot restore set"
    )
    assert restarted == ["winni"], restarted


# --------------------------------------------------------------- C1: stopping


def test_a_stop_can_leave_the_vm_in_the_boot_restore_set(
    project_home, monkeypatch
) -> None:
    """A maintenance job that stops a VM to patch it wants it back at boot."""
    _make_vm(project_home, "patched", running=True)
    monkeypatch.setattr(lifecycle, "list_vms", lambda: [])

    def fake_stop(name, **kwargs):
        # Whatever the real stop does to the bookkeeping, the flag has to be
        # passed through for the caller to be able to ask for it.
        if not kwargs.get("preserve_run_state"):
            meta = registry.load_meta(name)
            meta.last_run_state = "stopped"
            registry.save_meta(meta)
        return "graceful"

    monkeypatch.setattr(lifecycle, "stop", fake_stop)

    args = argparse.Namespace(
        names=["patched"],
        mode=None,
        force=False,
        quiet=False,
        preserve_run_state=True,
    )
    cmd_stop(args, _out([]))
    assert registry.load_meta("patched").last_run_state == "running"


def test_a_stop_says_when_it_cancels_the_boot_restore(project_home, monkeypatch) -> None:
    """It used to do it silently, which is the whole of the problem."""
    _make_vm(project_home, "patched", running=True)
    monkeypatch.setattr(lifecycle, "list_vms", lambda: [])

    def fake_stop(name, **kwargs):
        meta = registry.load_meta(name)
        meta.last_run_state = "stopped"
        registry.save_meta(meta)
        return "graceful"

    monkeypatch.setattr(lifecycle, "stop", fake_stop)

    lines: list[str] = []
    args = argparse.Namespace(
        names=["patched"], mode=None, force=False, quiet=False, preserve_run_state=False
    )
    cmd_stop(args, _out(lines))
    assert any("NOT be restored" in line for line in lines), lines


def test_a_stop_that_keeps_the_run_state_says_nothing_about_losing_it(
    project_home, monkeypatch
) -> None:
    _make_vm(project_home, "patched", running=True)
    monkeypatch.setattr(lifecycle, "list_vms", lambda: [])
    monkeypatch.setattr(lifecycle, "stop", lambda name, **kwargs: "graceful")

    lines: list[str] = []
    args = argparse.Namespace(
        names=["patched"], mode=None, force=False, quiet=False, preserve_run_state=True
    )
    cmd_stop(args, _out(lines))
    assert not any("NOT be restored" in line for line in lines), lines


def test_the_commands_tolerate_a_hand_built_namespace(project_home, monkeypatch) -> None:
    """The commands are called directly, with a namespace built by hand.

    selftest does exactly that, and it caught a crash: a command that reads
    a flag straight off the namespace raises AttributeError when the
    caller did not know about the flag. A new flag has to default to
    "off" for the callers that predate it.
    """
    from vmhub import cli

    _make_vm(project_home, "patched", running=True)
    monkeypatch.setattr(lifecycle, "list_vms", lambda: [])
    monkeypatch.setattr(lifecycle, "stop", lambda name, **kwargs: "graceful")

    args = argparse.Namespace(names=["patched"], force=False, mode=None, quiet=True)
    assert cli.cmd_stop(args, cli.Out()) == 0


# --------------------------------------------------------------- C4: the bundle


def test_a_bundle_is_only_named_once_it_is_whole(project_home, tmp_path, monkeypatch) -> None:
    """A half-written archive must never look like a backup.

    The archive used to be written straight to its final name, so a disk
    that filled up half-way through left a truncated file that `vmctl
    bundles` listed like any other backup.
    """
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"

    def explode(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export, "_build_tar", explode)

    with pytest.raises(OSError):
        export.export_vm("winni", dest_dir=out_dir)

    leftovers = sorted(p.name for p in out_dir.iterdir())
    assert leftovers == [], f"a failed export left files behind: {leftovers}"


def test_a_finished_bundle_records_its_checksum(project_home, tmp_path) -> None:
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"
    bundle = export.export_vm("winni", dest_dir=out_dir)

    sidecar = bundle.with_name(bundle.name + export.SIDECAR_SUFFIX)
    assert sidecar.is_file(), "the bundle has no checksum beside it"
    assert export._read_sidecar(bundle), "the checksum is empty"


def test_the_checksum_is_not_listed_as_a_backup(project_home, tmp_path) -> None:
    """It matches the same glob as the bundles, and it is not a bundle."""
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"
    export.export_vm("winni", dest_dir=out_dir)

    listed = export.list_bundles(out_dir)
    assert [entry["name"] for entry in listed] == ["winni"], listed


def test_a_damaged_bundle_is_reported_as_damaged(project_home, tmp_path) -> None:
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"
    bundle = export.export_vm("winni", dest_dir=out_dir)

    # Truncate it, the way a full disk would.
    data = bundle.read_bytes()
    bundle.write_bytes(data[: len(data) // 2])

    listed = export.list_bundles(out_dir, verify=True)
    assert listed, "the bundle disappeared from the listing"
    assert "error" in listed[0], listed[0]
    assert listed[0].get("verified") is False, listed[0]


def test_a_good_bundle_verifies(project_home, tmp_path) -> None:
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"
    export.export_vm("winni", dest_dir=out_dir)

    listed = export.list_bundles(out_dir, verify=True)
    assert listed[0].get("verified") is True, listed[0]
    assert "error" not in listed[0], listed[0]


def test_a_bundle_with_no_checksum_is_unverifiable_not_damaged(
    project_home, tmp_path
) -> None:
    """A bundle made before checksums existed is not corrupt."""
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"
    bundle = export.export_vm("winni", dest_dir=out_dir)
    bundle.with_name(bundle.name + export.SIDECAR_SUFFIX).unlink()

    listed = export.list_bundles(out_dir, verify=True)
    assert "error" not in listed[0], listed[0]
    assert listed[0].get("verified") is False, listed[0]


def test_a_full_disk_is_refused_before_the_copy_starts(
    project_home, tmp_path, monkeypatch
) -> None:
    _make_vm(project_home, "winni")
    out_dir = tmp_path / "out"

    monkeypatch.setattr(export, "_free_bytes", lambda directory: 1)

    with pytest.raises(Exception) as caught:
        export.export_vm("winni", dest_dir=out_dir)
    assert "not enough space" in str(caught.value), str(caught.value)
    assert sorted(p.name for p in out_dir.iterdir()) == []


# --------------------------------------------------------------- H1: the unit


def test_the_unit_is_not_killed_by_the_default_systemd_timeout() -> None:
    """systemd's default TimeoutStartSec is 90 seconds.

    The restore starts the VMs one at a time and each one waits up to 90
    seconds for QEMU, so the default killed the unit part-way through the
    fleet and left the rest of it down.
    """
    unit = autostart.systemd_unit_text()
    assert "TimeoutStartSec=0" in unit, unit
    assert "TimeoutStopSec=0" in unit, unit


# --------------------------------------------------------------- H3: the verdict


def test_an_install_that_cannot_work_at_boot_is_not_a_success(
    project_home, monkeypatch
) -> None:
    """The unit was written, and the install reported success.

    The message said the VMs would not come back; the exit code did not
    say it. A provisioning script reads the exit code.
    """
    monkeypatch.setattr(autostart, "detect_init", lambda: "systemd")
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    monkeypatch.setattr(autostart, "_systemd_unit_path", lambda: Path("/tmp/vmhub.service"))
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    _fake_account(monkeypatch)

    result = autostart.install(target="systemd")
    assert result.problems, "an install that cannot work at boot reported no problem"
    assert not result.ok


def test_an_install_whose_lingering_failed_is_not_a_success(
    project_home, monkeypatch
) -> None:
    monkeypatch.setattr(autostart, "detect_init", lambda: "systemd")
    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        autostart, "_systemd_unit_path", lambda: Path("/tmp/vmhub.service")
    )
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    _fake_account(monkeypatch)
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="denied"),
    )

    result = autostart.install(target="systemd")
    assert not result.ok
    assert any("linger" in problem for problem in result.problems), result.problems


def test_the_install_result_still_reads_as_a_list_of_messages(
    project_home, monkeypatch
) -> None:
    """So a caller that only wants the messages is not broken."""
    monkeypatch.setattr(autostart, "detect_init", lambda: "systemd")
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    monkeypatch.setattr(autostart, "_systemd_unit_path", lambda: Path("/tmp/vmhub.service"))
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    _fake_account(monkeypatch)

    result = autostart.install(target="systemd")
    assert list(result) == result.messages


# --------------------------------------------------------------- H4: the owner


def test_the_owner_may_not_be_root() -> None:
    """Rootless podman keeps its store under the owner's home.

    A unit installed for root starts nothing at boot, and every message says
    it worked.
    """
    with pytest.raises(Exception) as caught:
        autostart.resolve_owner("root")
    assert "root" in str(caught.value), str(caught.value)


def test_an_owner_that_is_not_a_real_account_is_refused() -> None:
    with pytest.raises(Exception) as caught:
        autostart.resolve_owner("nosuchuser")
    assert "nosuchuser" in str(caught.value), str(caught.value)


def test_an_explicit_owner_is_used(project_home, monkeypatch) -> None:
    """The flag, so a cron job can say who the VMs belong to."""
    monkeypatch.setattr(autostart, "detect_init", lambda: "session")
    monkeypatch.setattr(
        autostart, "_session_autostart_path", lambda: Path("/tmp/vmhub.desktop")
    )
    monkeypatch.setattr(autostart.pwd, "getpwnam", lambda n: SimpleNamespace(pw_dir="/home/admin"))

    result = autostart.install(target="session", owner="admin")
    assert result.ok, result.problems


def test_the_owner_comes_from_the_environment_when_it_can(
    project_home, monkeypatch
) -> None:
    monkeypatch.setenv(autostart.OWNER_ENV, "admin")
    assert autostart._vm_owner() == "admin"
    monkeypatch.delenv(autostart.OWNER_ENV)
    monkeypatch.setenv("SUDO_USER", "someone")
    assert autostart._vm_owner() == "someone"


def test_the_status_output_names_the_owner(project_home, monkeypatch) -> None:
    monkeypatch.setattr(autostart, "detect_init", lambda: "none")
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    monkeypatch.setattr(
        autostart, "_systemd_unit_path", lambda: Path("/tmp/nope.service")
    )
    monkeypatch.setattr(
        autostart, "_session_autostart_path", lambda: Path("/tmp/nope.desktop")
    )
    state = autostart.status()
    assert state["owner"] == "admin", state
