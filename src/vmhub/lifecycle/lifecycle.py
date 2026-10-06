"""
lifecycle: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

import shutil, time

from pathlib import Path

from .. import blueprints, disk, paths, podman, ports, qga, qmp, registry, spec

from ..errors import (
    AlreadyExists,
    DiskError,
    LifecycleError,
    NotFound,
    QgaError,
    QmpError,
    TemplateInUse,
    VmRunning,
    VmhubError,
)

from .progress import Progress, _noop

from .clone import flatten

from .status import _disk_has_data, disk_has_data, list_vms, status, template_disk


def create(
    name: str,
    *,
    blueprint: str | None = None,
    cpus: int | None = None,
    ram: str | None = None,
    disk_size: str | None = None,
    disk_type: str | None = None,
    boot_mode: str | None = None,
    boot_iso: str | None = None,
    image: str | None = None,
    ports_map: dict[str, int] | None = None,
    progress: Progress = _noop,
) -> registry.VmMeta:
    progress = progress or _noop
    spec.validate_name(name)
    if registry.exists(name):
        raise AlreadyExists(f"VM already exists: {name}")

    progress(f"Creating {name}...")
    if blueprint:
        vm_spec = blueprints.load(blueprint).base_spec(name)
    else:
        vm_spec = spec.VmSpec(name=name)

    if cpus is not None:
        vm_spec.resources.cpus = cpus
    if ram is not None:
        vm_spec.resources.ram = ram
    if disk_size is not None:
        vm_spec.resources.disk = disk_size
    if disk_type is not None:
        vm_spec.resources.disk_type = disk_type
    if boot_mode is not None:
        vm_spec.boot.mode = boot_mode
    if boot_iso is not None:
        vm_spec.boot.iso = boot_iso
    if image is not None:
        repository, tag = spec.split_image_ref(image)
        vm_spec.image = spec.Image(
            repository=repository or spec.DEFAULT_REPO,
            tag=tag or spec.DEFAULT_TAG,
        )
    progress("Allocating host ports...")
    allocated = ports.allocate_all()
    wanted = ports_map or {}
    net = vm_spec.network
    net.set_role("web", 8006, int(wanted.get("web") or allocated["web"]))
    # Only publish a port whose guest service is actually forwarded, otherwise the
    # host port forwards to a passt listener that will never answer.
    ssh_host = int(wanted.get("ssh") or allocated["ssh"]) if 22 in net.guest_ports else 0
    if ssh_host:
        net.set_role("ssh", 22, ssh_host)
    rdp_host = (
        int(wanted.get("rdp") or allocated["rdp"])
        if 3389 in net.guest_ports or wanted.get("rdp")
        else 0
    )
    if rdp_host:
        net.set_role("rdp", 3389, rdp_host)

    vm_spec.validate()
    paths.storage_dir(name).mkdir(parents=True, exist_ok=True)
    paths.disk_dir(name).mkdir(parents=True, exist_ok=True)
    progress(f"Creating {vm_spec.resources.disk} qcow2 disk...")
    disk.create(paths.disk_path(name), vm_spec.resources.disk_bytes)
    registry.save_spec(vm_spec)
    meta = registry.create_meta(
        name,
        blueprint=vm_spec.blueprint,
        image=vm_spec.image.ref,
    )
    published = [f"{port.label} :{port.host}" for port in vm_spec.network.ports if port.host]
    progress(f"Created {name} ({', '.join(published)})")
    return meta


def finish_install(vm: str, *, progress: Progress = _noop) -> list[str]:
    """Mark a guest as installed: boot from the disk, not from any optical media.

    Clears the auto-download boot image, detaches the install ISO, and — once the
    disk actually holds an installed system — removes the cached fallback image the
    container fetched on first start, so the boot chain is the disk and nothing
    else. Leaving an installer attached is the classic trap: it is attached at
    the highest boot priority, so the machine shows the install media's
    "press any key" prompt every boot and can fall through to another image.
    """
    actions: list[str] = []
    progress = progress or _noop
    vm_spec = registry.load_spec(vm)

    if vm_spec.boot.iso:
        was = vm_spec.boot.iso
        progress(f"Clearing boot.iso ({was!r})")
        vm_spec.boot.iso = ""
        actions.append(f"cleared boot.iso (was {was!r})")

    if vm_spec.media.install:
        name = Path(vm_spec.media.install).name
        progress(f"Detaching the install ISO {name}")
        vm_spec.media.install = ""
        actions.append(f"detached install ISO {name}")

    registry.save_spec(vm_spec)

    installed = disk_has_data(vm)
    cached = paths.storage_dir(vm) / "boot.iso"
    if installed and cached.is_file():
        progress(f"Removing the cached fallback image {cached.name} ({spec.format_size(cached.stat().st_size)})")
        try:
            cached.unlink()
            actions.append(f"removed cached fallback {cached.name}")
        except OSError as exc:
            progress(f"  could not remove {cached.name}: {exc}")
    elif not installed:
        progress(
            "The disk is still empty, so the guest does not look installed yet. "
            "The cached fallback image was kept."
        )

    if not actions:
        progress(f"{vm} already boots from its disk")
    else:
        progress(f"{vm}: " + "; ".join(actions) + ". Restart it to boot from the disk.")
    return actions


def _needs_recreate(vm: str, vm_spec: spec.VmSpec, meta: registry.VmMeta) -> bool:
    if not podman.exists(vm):
        return True
    try:
        data = podman.inspect(vm)
    except Exception:
        return True
    labels = ((data.get("Config") or {}).get("Labels")) or {}
    current = podman.spec_hash(vm_spec, meta.uuid, meta.mac, template_disk(vm))
    return labels.get(podman.LABEL_SPEC) != current


def _assert_disk_adopted(vm: str, progress: Progress = _noop) -> None:
    progress = progress or _noop
    stray = sorted(paths.storage_dir(vm).glob(f"*/{paths.DISK_NAME}"))
    if stray:
        for path in stray:
            progress(f"removing stray container-created disk: {path}")
            try:
                path.unlink()
            except OSError:
                pass
        raise LifecycleError(
            f"{vm}: the container created its own disk at {stray[0]} instead of using "
            f"{paths.disk_path(vm)}. This means the boot image changed, which moves the "
            f"container's storage subdirectory. The stray disk was removed; re-run start."
        )

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with qmp.Qmp(paths.qmp_path(vm), timeout=5) as client:
                files = [str(node.get("file", "")) for node in client.block_backends()]
        except Exception:
            time.sleep(1)
            continue
        if any("data.qcow2" in item for item in files):
            return
        time.sleep(1)
    progress(
        "warning: could not confirm via QMP which disk QEMU attached; "
        "check the console looks right"
    )


def _guard_locked_disk(vm: str, meta: registry.VmMeta) -> None:
    if not meta.is_template:
        if meta.template_source and podman.is_running(meta.template_source):
            raise TemplateInUse(
                f"{vm} is a linked clone of {meta.template_source}, which is running. "
                f"The template must be stopped: QEMU cannot open a disk for writing "
                f"while the template holds it. Either stop {meta.template_source}, or "
                f"make this clone independent with: vmctl clone <new> {vm} --mode full"
            )
        return
    live = [
        name
        for name in meta.dependents
        if registry.exists(name) and podman.is_running(name)
    ]
    if live:
        raise TemplateInUse(
            f"{vm} is a template and cannot run while its clones are running: "
            f"{', '.join(sorted(live))}. The clone holds a read lock on the "
            f"template's disk, so QEMU cannot open it read-write. Stop the clones "
            f"first, or flatten a clone to break the dependency."
        )


def start(vm: str, *, progress: Progress = _noop) -> str:
    progress = progress or _noop
    meta = registry.load_meta(vm)
    _guard_locked_disk(vm, meta)
    vm_spec = registry.load_spec(vm)

    if not podman.is_running(vm):
        for change in ports.ensure_for(vm):
            progress(f"port repaired: {change}")
        vm_spec = registry.load_spec(vm)
    for warning in ports.verify(vm_spec, check_busy=not podman.is_running(vm)):
        progress(f"warning: {warning}")
    if vm_spec.media.install and vm_spec.boot.iso:
        progress(
            f"warning: {vm} has both an attached install ISO and boot.iso="
            f"{vm_spec.boot.iso!r}. The container will download {vm_spec.boot.iso!r} "
            f"as well. Clear it with: vmctl set {vm} boot.iso=  (or re-attach the ISO)"
        )
    disk_file = paths.disk_path(vm)
    if disk_file.is_file() and not _disk_has_data(vm) and not vm_spec.boot.iso:
        progress(
            f"warning: {vm}'s disk is empty and boot.iso is unset, so the container "
            f"will download an Alpine Linux image (~60 MB) and attach it. Anything "
            f"already attached competes with it for boot order -- if that does not "
            f"boot, you will land in Alpine. Set boot.iso to choose your own media."
        )

    if _needs_recreate(vm, vm_spec, meta):
        if podman.exists(vm):
            progress("Configuration changed, recreating container...")
            podman.rm(vm)
        progress("Pulling image if needed...")
        if not podman.image_present(vm_spec.image.ref):
            podman.pull(vm_spec.image.ref)
        progress("Creating container...")
        podman.create(vm_spec, meta.uuid, meta.mac, template_disk(vm))
    else:
        progress("Starting container...")
        podman.start(vm)

    container = podman.container_name(vm)
    _await_qemu(vm, progress)
    _assert_disk_adopted(vm, progress)
    meta.last_run_state = "running"
    registry.save_meta(meta)
    progress(f"{vm} is running")
    return container


QEMU_READY_TIMEOUT = 90


QEMU_READY_STATES = {"running", "paused", "prelaunch", "suspended", "inmigrate"}


def _await_qemu(vm: str, progress: Progress = _noop) -> None:
    """Block until QEMU is actually serving, or fail loudly.

    The container being 'running' is not evidence that QEMU started: QEMU can die
    minutes in, after firmware init, typically because it could not open a disk
    (a locked backing file, a missing ISO). Waiting for the QMP socket to answer
    is the only reliable readiness signal.
    """
    progress = progress or _noop
    deadline = time.monotonic() + QEMU_READY_TIMEOUT
    while time.monotonic() < deadline:
        state = podman.state(vm)
        if state in {"exited", "absent"}:
            raise LifecycleError(_startup_failure(vm))
        socket_path = paths.qmp_path(vm)
        if socket_path.exists():
            try:
                with qmp.Qmp(socket_path, timeout=5) as client:
                    if client.query_status() in QEMU_READY_STATES:
                        return
            except (QmpError, OSError):
                pass
        time.sleep(2)
    raise LifecycleError(
        f"{vm}: QEMU did not become ready within {QEMU_READY_TIMEOUT}s. "
        f"Check the Logs tab for details."
    )


def _startup_failure(vm: str) -> str:
    tail = podman.logs(vm, tail=25) if podman.exists(vm) else ""
    interesting = [
        line
        for line in tail.splitlines()
        if any(token in line for token in ("ERROR", "error:", "lock", "Could not", "failed", "Exit"))
    ]
    detail = "\n".join(interesting[-6:]) if interesting else tail[-800:]
    return f"{vm} failed to stay running.\nContainer log:\n{detail}"


STOP_MODES = ("guest", "graceful", "power", "force")


def _mark_stopped(vm: str) -> None:
    if not registry.exists(vm):
        return
    meta = registry.load_meta(vm)
    if meta.last_run_state != "stopped":
        meta.last_run_state = "stopped"
        registry.save_meta(meta)


def stop(
    vm: str,
    *,
    force: bool = False,
    mode: str | None = None,
    progress: Progress = _noop,
) -> str:
    """Stop a VM.

    guest     Ask the in-guest agent to shut down (QGA guest-shutdown). This is
              the only clean stop for Windows, which ignores the container's ACPI
              signal; it needs qemu-guest-agent installed in the guest.
    graceful  ACPI shutdown; waits up to shutdown.timeout seconds for the guest
              to respond. Safe, but a live installer ISO never answers ACPI and
              will always take the full timeout.
    power     QMP quit: cut power to the guest immediately. The container still
              shuts down cleanly and is removed, but the guest is not given the
              chance to flush its filesystems.
    force     SIGKILL the container. Nothing is flushed anywhere and no cleanup
              runs; the guest filesystem may be left inconsistent.
    """
    progress = progress or _noop
    if mode is None:
        mode = "force" if force else "graceful"
    if mode not in STOP_MODES:
        raise LifecycleError(f"unknown stop mode {mode!r} (expected one of {', '.join(STOP_MODES)})")

    _mark_stopped(vm)
    if not podman.exists(vm):
        progress(f"{vm} has no container")
        return mode
    if not podman.is_running(vm):
        progress(f"{vm} is already stopped")
        return mode

    if mode == "force":
        progress(f"Force-killing {vm} (no guest shutdown, no cleanup)...")
        podman.kill(vm)
        return mode

    if mode == "guest":
        progress(f"Asking the guest agent in {vm} to shut down...")
        try:
            qga.shutdown(vm)
        except (QgaError, NotFound, OSError) as exc:
            raise LifecycleError(
                f"{vm}: the guest agent did not answer ({exc}). Install "
                f"qemu-guest-agent in the guest, or use --mode graceful / --mode power."
            ) from exc
        grace = 30
        try:
            grace = registry.load_spec(vm).shutdown.podman_grace
        except Exception:
            pass
        podman.stop(vm, timeout=grace)
        return mode

    if mode == "power":
        progress(f"Cutting power to {vm} via QMP...")
        try:
            with qmp.Qmp(paths.qmp_path(vm), timeout=5) as client:
                client.quit()
        except (QmpError, NotFound) as exc:
            progress(f"  QMP unavailable ({exc}); falling back to SIGKILL")
            podman.kill(vm)
            return mode
        try:
            podman.wait_until(vm, {"exited", "absent"}, timeout=20)
        finally:
            if podman.exists(vm) and podman.is_running(vm):
                podman.kill(vm)
        return mode

    grace = 20
    try:
        grace = registry.load_spec(vm).shutdown.podman_grace
    except Exception:
        pass
    progress(f"Stopping {vm} (ACPI shutdown, up to {grace - 20}s)...")
    podman.stop(vm, timeout=grace)
    return mode


def remove(vm: str, *, keep_disk: bool = False, progress: Progress = _noop) -> None:
    progress = progress or _noop
    if not registry.exists(vm):
        raise NotFound(f"no such VM: {vm}")
    if podman.is_running(vm):
        raise VmRunning(f"{vm} is running — stop it first")
    meta = registry.load_meta(vm)
    dependents = registry.dependents_of(vm)
    if dependents:
        # A linked clone's disk has this VM's disk as its backing file.
        # Deleting the source without resolving that first would leave every
        # clone pointing at a file that no longer exists, so make them
        # standalone while the backing file is still there.
        for dep in dependents:
            if podman.is_running(dep):
                raise VmRunning(
                    f"{dep} is a linked clone of {vm} and is running; "
                    f"stop it before removing {vm}"
                )
        for dep in dependents:
            progress(f"{dep} is a linked clone of {vm}; making it standalone...")
            flatten(dep, progress=progress)
        progress(
            f"Resolved {len(dependents)} linked clone(s) of {vm} into standalone disks"
        )

    if meta.template_source:
        registry.release_dependent(vm)

    progress(f"Removing {vm}...")
    podman.rm(vm)
    from .. import rdp

    rdp.remove_profile(vm)
    if keep_disk:
        # Keep the VM registered. Deleting meta.json would leave a directory that
        # discovery cannot see, so the disk would silently become unreachable.
        if meta.template_source:
            flatten(vm, progress=progress)
        progress(
            f"Kept {vm}: the container is gone but the VM and its disk remain, "
            f"so it can still be started with 'vmctl start {vm}'"
        )
        return
    shutil.rmtree(paths.vm_dir(vm), ignore_errors=True)
    registry.delete_meta(vm)
    progress(f"Removed {vm}")


def resize(vm: str, size: str, *, progress: Progress = _noop) -> None:
    progress = progress or _noop
    spec.parse_size(size)
    vm_spec = registry.load_spec(vm)
    if podman.is_running(vm):
        raise VmRunning(f"stop {vm} before resizing its disk")
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk yet")
    current = disk.virtual_size(disk_file)
    requested = spec.parse_size(size)
    if requested < current:
        raise DiskError(
            f"shrinking is not supported "
            f"({spec.format_size(current)} -> {size})"
        )
    if requested > current:
        progress(f"Growing {vm} disk {spec.format_size(current)} -> {size}...")
        disk.run(["resize", "-f", "qcow2", str(disk_file), str(requested)], timeout=1800)
    vm_spec.resources.disk = size
    registry.save_spec(vm_spec)
    progress(
        f"{vm} disk is now {size}"
        + ("" if requested > current else " (already that size)")
    )


def restore_run_state(progress: Progress = _noop) -> list[str]:
    progress = progress or _noop
    restored: list[str] = []
    for meta in registry.iter_metas():
        if meta.last_run_state != "running":
            continue
        if podman.is_running(meta.name):
            continue
        try:
            start(meta.name, progress=lambda _m: None)
            restored.append(meta.name)
        except Exception as exc:
            progress(f"could not restore {meta.name}: {exc}")
    if restored:
        progress(f"Restored {len(restored)} previously running VM(s): {', '.join(restored)}")
    return restored


def stop_all(
    *, force: bool = False, mode: str | None = None, progress: Progress = _noop
) -> list[str]:
    progress = progress or _noop
    stopped: list[str] = []
    for status in list_vms():
        if not status.powered:
            continue
        try:
            stop(status.name, force=force, mode=mode, progress=progress)
            stopped.append(status.name)
        except VmhubError as exc:
            progress(f"could not stop {status.name}: {exc}")
    return stopped
