"""
snapshot: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

from typing import Any

from .. import disk, paths, podman, qmp, registry, spec

from ..errors import (
    AlreadyExists,
    DiskError,
    NotFound,
    VmNotRunning,
)

from .progress import Progress, _noop

from .lifecycle import start, stop


def list_snapshots(vm: str) -> list[dict[str, Any]]:
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        return []
    return disk.snapshots(disk_file)


def take_snapshot(vm: str, name: str, *, progress: Progress = _noop) -> str:
    progress = progress or _noop
    spec.validate_name(name)
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk yet")
    if not disk.supports_internal_snapshots(disk_file):
        raise DiskError(
            f"{vm} disk is {disk.format_of(disk_file)}; internal snapshots require qcow2"
        )
    existing = disk.snapshot_names(disk_file)
    if name in existing:
        raise AlreadyExists(f"snapshot already exists: {name}")

    running = podman.is_running(vm)
    progress(f"Taking snapshot {name} on {vm}...")
    if running:
        with qmp.Qmp(paths.qmp_path(vm)) as client:
            if not client.is_running():
                raise VmNotRunning(
                    f"{vm} guest is not running (QMP status not 'running')"
                )
            device = qmp.primary_block_device(client)
            client.take_snapshot(device, name)
    else:
        # No QEMU to talk to, so snapshot the image directly. Taking one
        # while powered off is exactly when a clean install is worth
        # protecting, and VMware allows it.
        disk.create_snapshot(disk_file, name)

    meta = registry.load_meta(vm)
    meta.snapshot_power[name] = running
    registry.save_meta(meta)
    progress(
        f"Snapshot {name} created on {vm} "
        f"({'running' if running else 'powered off'})"
    )
    return name


def delete_snapshot(vm: str, name: str, *, progress: Progress = _noop) -> None:
    progress = progress or _noop
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk yet")
    if name not in disk.snapshot_names(disk_file):
        raise NotFound(f"no such snapshot: {name}")
    if podman.is_running(vm):
        progress(f"Deleting snapshot {name} from running VM {vm}...")
        with qmp.Qmp(paths.qmp_path(vm)) as client:
            device = qmp.primary_block_device(client)
            client.delete_snapshot(device, name)
    else:
        progress(f"Deleting snapshot {name} from {vm}...")
        disk.delete_snapshot(disk_file, name)
    meta = registry.load_meta(vm)
    if meta.snapshot_power.pop(name, None) is not None:
        registry.save_meta(meta)
    progress(f"Deleted snapshot {name}")


def revert(
    vm: str,
    name: str,
    *,
    progress: Progress = _noop,
    restart: bool | None = None,
) -> None:
    """Roll the disk back to `name`, discarding anything taken after it.

    restart=None restores the power state the snapshot was taken in, which
    is what VMware does. Pass True or False to override it. A snapshot
    taken before power states were recorded falls back to the old rule of
    restarting only if the VM was running when you reverted.
    """
    progress = progress or _noop
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk yet")
    ordered = disk.snapshot_names(disk_file)
    if name not in ordered:
        raise NotFound(f"no such snapshot: {name}")

    was_running = podman.is_running(vm)
    if was_running:
        progress("Stopping VM (revert is an offline operation)...")
        stop(vm, progress=progress)

    newer = ordered[ordered.index(name) + 1 :]
    progress(f"Reverting {vm} to snapshot {name}...")
    disk.apply_snapshot(disk_file, name)

    meta = registry.load_meta(vm)
    if newer:
        for stale in newer:
            meta.snapshot_power.pop(stale, None)

    for stale in newer:
        progress(f"Discarding newer snapshot {stale}...")
        try:
            disk.delete_snapshot(disk_file, stale)
        except (NotFound, DiskError) as exc:
            progress(f"  could not discard {stale}: {exc}")

    progress(f"{vm} reverted to {name}")
    if newer:
        progress(f"Discarded {len(newer)} snapshot(s) taken after {name}")
    recorded = meta.snapshot_power.get(name)
    should_restart = (recorded if recorded is not None else was_running) \
        if restart is None else restart
    meta.snapshot_power[name] = should_restart
    registry.save_meta(meta)
    if should_restart:
        progress(
            f"Restoring the power state this snapshot was taken in "
            f"({'running' if should_restart else 'off'})"
        )
        start(vm, progress=progress)
