"""
media: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

from pathlib import Path

from typing import Any

from .. import iso, podman, registry

from .progress import Progress, _noop


def attach_iso(
    vm: str,
    slot: str,
    source: Path,
    *,
    progress: Progress = _noop,
) -> iso.IsoInfo:
    progress = progress or _noop
    iso.slot_path(slot)
    vm_spec = registry.load_spec(vm)
    source = Path(source).expanduser()
    progress(f"Checking {source.name}...")
    info = iso.validate(source)

    if podman.is_running(vm):
        progress(f"{vm} is running; the ISO is applied on next start")
    else:
        progress(f"{vm} is stopped; the ISO will be used on next start")

    if slot == iso.INSTALL_SLOT:
        cleared = False
        if vm_spec.boot.iso:
            cleared = vm_spec.boot.iso
            vm_spec.boot.iso = ""
            progress(
                f"Cleared boot.iso ({cleared!r}) — an attached install ISO takes "
                f"precedence over an auto-downloaded boot image."
            )
        vm_spec.media.install = str(source.resolve())
        note = (
            f"{source.name} will boot before the disk ({info.describe()}). "
            f"Detach it once installation is finished, or the VM will boot the ISO again."
        )
    else:
        vm_spec.media.drivers = str(source.resolve())
        note = (
            f"{source.name} is attached as a second CD-ROM ({info.describe()}). "
            f"Use it in the guest to install drivers such as virtio."
        )
    registry.save_spec(vm_spec)
    progress(f"Attached to {vm}: {source.name}")
    progress(note)
    return info


def detach_iso(vm: str, slot: str, *, progress: Progress = _noop) -> bool:
    progress = progress or _noop
    iso.slot_path(slot)
    vm_spec = registry.load_spec(vm)
    current = vm_spec.media_path(slot)
    if not current:
        progress(f"{vm} has no {slot} ISO attached")
        return False
    if slot == iso.INSTALL_SLOT:
        vm_spec.media.install = ""
    else:
        vm_spec.media.drivers = ""
    registry.save_spec(vm_spec)
    progress(f"Detached {Path(current).name} from {vm} (applies on next start)")
    return True


def iso_status(vm: str) -> list[dict[str, Any]]:
    vm_spec = registry.load_spec(vm)
    entries: list[dict[str, Any]] = []
    for slot in iso.SLOTS:
        host = vm_spec.media_path(slot)
        if not host:
            entries.append({"slot": slot, "label": iso.SLOT_LABELS[slot],
                            "attached": False, "path": "", "info": None, "present": False})
            continue
        path = Path(host).expanduser()
        present = path.is_file()
        entries.append(
            {
                "slot": slot,
                "label": iso.SLOT_LABELS[slot],
                "attached": True,
                "path": str(path),
                "present": present,
                "info": iso.probe(path) if present else None,
            }
        )
    return entries
