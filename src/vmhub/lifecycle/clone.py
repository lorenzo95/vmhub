"""
clone: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

from .. import disk, paths, podman, ports, registry, spec

from ..errors import (
    AlreadyExists,
    LifecycleError,
    NotFound,
    TemplateInUse,
    VmRunning,
)

from .progress import Progress, _noop

from .fsutil import os_replace


CLONE_MODES = ("linked", "full")


def clone(
    name: str,
    source: str,
    *,
    mode: str = "linked",
    cpus: int | None = None,
    ram: str | None = None,
    progress: Progress = _noop,
) -> registry.VmMeta:
    """Clone a VM.

    linked  A copy-on-write overlay on the source disk: created instantly, costing
            only the blocks the clone writes. It references the source, so the
            source must be a template — it cannot run while a clone does, and it
            cannot be deleted until the clone is flattened.
    full    A standalone copy of the source disk. Slower and it copies real data,
            but the clone is completely independent: it can run alongside the
            source, be exported, and outlive it. Any stopped VM can be the source.
    """
    progress = progress or _noop
    if mode not in CLONE_MODES:
        raise LifecycleError(
            f"unknown clone mode {mode!r} (expected one of {', '.join(CLONE_MODES)})"
        )
    spec.validate_name(name)
    if registry.exists(name):
        raise AlreadyExists(f"VM already exists: {name}")
    source_meta = registry.load_meta(source)
    if mode == "linked" and not source_meta.is_template:
        raise LifecycleError(
            f"{source} is not a template, and a linked clone references its disk. "
            f"Either mark it first (vmctl template mark {source}) or copy it "
            f"instead: vmctl clone {name} {source} --mode full"
        )
    if podman.is_running(source):
        raise TemplateInUse(
            f"{source} is running — stop it before cloning, its disk is locked "
            "while it is being read"
        )
    source_disk = paths.disk_path(source)
    if not disk.exists(source_disk):
        raise NotFound(f"{source} has no disk: {source_disk}")

    progress(f"Reading {source}...")
    vm_spec = registry.load_spec(source)
    vm_spec.name = name
    vm_spec.blueprint = source_meta.blueprint
    vm_spec.boot.iso = ""
    if cpus is not None:
        vm_spec.resources.cpus = cpus
    if ram is not None:
        vm_spec.resources.ram = ram

    progress("Allocating host ports...")
    allocated = ports.allocate_all()
    vm_spec.network.set_role("web", 8006, allocated["web"])
    if 22 in vm_spec.network.guest_ports:
        vm_spec.network.set_role("ssh", 22, allocated["ssh"])
    if 3389 in vm_spec.network.guest_ports:
        vm_spec.network.set_role("rdp", 3389, allocated["rdp"])

    clone_disk = paths.disk_path(name)
    paths.storage_dir(name).mkdir(parents=True, exist_ok=True)
    paths.disk_dir(name).mkdir(parents=True, exist_ok=True)
    if mode == "full":
        used = disk.chain_used(source_disk)
        progress(
            f"Copying {source}'s disk ({spec.format_size(used)} in use); "
            f"this reads the whole image and takes a while..."
        )
        disk.convert(source_disk.resolve(), clone_disk, "qcow2", compress=False)
    else:
        progress("Creating instant copy-on-write clone (no data copied)...")
        disk.create_overlay(clone_disk, source_disk.resolve())

    vm_spec.resources.disk = spec.format_size(disk.virtual_size(clone_disk))
    registry.save_spec(vm_spec)
    meta = registry.create_meta(
        name,
        blueprint=source_meta.blueprint,
        image=source_meta.image,
        template_source=None if mode == "full" else source,
    )
    if mode == "linked":
        registry.register_dependent(name, source)
        note = f"shares storage with {source}"
    else:
        note = "fully independent of the source"
    progress(
        f"Cloned {source} -> {name} ({mode}, "
        f"{spec.format_size(disk.actual_size(clone_disk))} on disk, {note})"
    )
    return meta


def mark_template(vm: str, *, progress: Progress = _noop) -> None:
    progress = progress or _noop
    meta = registry.load_meta(vm)
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk yet — boot and install something first")
    if meta.template_source:
        raise LifecycleError(f"{vm} is itself a clone; flatten it before marking as a template")
    if podman.is_running(vm):
        raise VmRunning(f"stop {vm} before marking it as a template")
    meta.is_template = True
    registry.save_meta(meta)
    progress(f"{vm} marked as template (usable as a clone source)")


def unmark_template(vm: str, *, progress: Progress = _noop) -> None:
    progress = progress or _noop
    meta = registry.load_meta(vm)
    meta.is_template = False
    registry.save_meta(meta)
    progress(f"{vm} is no longer a template")


def flatten(vm: str, *, progress: Progress = _noop) -> None:
    progress = progress or _noop
    meta = registry.load_meta(vm)
    if not meta.template_source:
        return
    source = meta.template_source
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk")
    if podman.is_running(vm):
        raise VmRunning(f"stop {vm} before flattening")

    progress(f"Resolving backing chain into a standalone image (this copies used data)...")
    if podman.exists(vm):
        podman.rm(vm)
    temp = disk_file.with_suffix(".flatten.qcow2")
    if temp.exists():
        temp.unlink()
    used = disk.actual_size(disk_file)
    disk.convert(disk_file, temp, "qcow2")
    os_replace(temp, disk_file)
    registry.release_dependent(vm)
    progress(
        f"{vm} is now self-contained ({spec.format_size(used)} was in use, "
        f"{spec.format_size(disk.actual_size(disk_file))} now on disk)"
    )


def rebase_all(template: str, *, progress: Progress = _noop) -> list[str]:
    progress = progress or _noop
    dependents = registry.dependents_of(template)
    if not dependents:
        return []
    source_disk = paths.disk_path(template).resolve()
    if not disk.exists(source_disk):
        raise NotFound(f"template has no disk: {source_disk}")
    if podman.is_running(template):
        raise TemplateInUse(f"stop the template {template} before rebasing its clones")
    for clone_name in dependents:
        if podman.is_running(clone_name):
            raise TemplateInUse(f"stop clone {clone_name} before rebasing")
    for clone_name in dependents:
        progress(f"Rebasing {clone_name} onto {template}...")
        disk.rebase(paths.disk_path(clone_name), source_disk)
    return dependents
