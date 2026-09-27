from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import blueprints, disk, iso, paths, podman, ports, qga, qmp, registry, spec
from .errors import (
    AlreadyExists,
    DiskError,
    QgaError,
    QmpError,
    LifecycleError,
    NotFound,
    TemplateInUse,
    VmNotRunning,
    VmRunning,
)

Progress = Callable[[str], None] | None


def _noop(message: str) -> None:
    pass


@dataclass
class VmStatus:
    name: str
    exists: bool = True
    container: str = "absent"
    powered: bool = False
    paused: bool = False
    cpus: int = 0
    ram: str = ""
    disk_size: str = ""
    disk_actual: str = ""
    is_template: bool = False
    template_source: str | None = None
    dependents: list[str] = field(default_factory=list)
    boot_mode: str = "uefi"
    disk_type: str = "scsi"
    ports: dict[int, int] = field(default_factory=dict)
    guest_ports: list[int] = field(default_factory=list)
    has_disk: bool = False
    backing: str | None = None
    snapshots: list[str] = field(default_factory=list)
    blueprint: str = ""
    console_url: str = ""
    viewer_port: int = 0
    kvm: bool = True
    boot_image: str = ""
    has_disk_data: bool = False
    peer_reachable: bool = False
    peer_targets: list[str] = field(default_factory=list)
    live_media: list[str] = field(default_factory=list)
    drifted: bool = False
    notices: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.exists:
            return "missing"
        if self.paused:
            return "paused"
        if self.powered:
            return "running"
        return "stopped"


@dataclass
class Ctx:
    """Everything a refresh needs, fetched once and shared by every status().

    status() used to re-derive the same shared facts for each VM — the host's
    addresses, every spec, every container — and spawn two or three podman
    commands per VM. One refresh of seven VMs cost 1.6 s and 69 subprocesses,
    and it ran on the GTK main thread. This holds the answers for one pass.
    """

    containers: dict[str, podman.Container] = field(default_factory=dict)
    specs: dict[str, spec.VmSpec] = field(default_factory=dict)
    metas: dict[str, registry.VmMeta] = field(default_factory=dict)
    disk_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    has_data: dict[str, bool] = field(default_factory=dict)
    live: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    addresses: list[tuple[str, str]] = field(default_factory=list)
    shadowed: str = ""
    _peer: str | None = None

    def container(self, vm: str) -> podman.Container | None:
        return self.containers.get(vm)

    def state(self, vm: str) -> str:
        container = self.containers.get(vm)
        return container.state if container else "absent"

    def powered(self, vm: str) -> bool:
        container = self.containers.get(vm)
        return bool(container and container.powered)

    def vm_spec(self, vm: str) -> spec.VmSpec:
        if vm not in self.specs:
            self.specs[vm] = registry.load_spec(vm)
        return self.specs[vm]

    def meta(self, vm: str) -> registry.VmMeta:
        if vm not in self.metas:
            self.metas[vm] = registry.load_meta(vm)
        return self.metas[vm]

    def info(self, vm: str) -> dict[str, Any]:
        """One qemu-img info per VM, shared by snapshots/backing/format."""
        if vm not in self.disk_info:
            self.disk_info[vm] = disk.cached_info(paths.disk_path(vm))
        return self.disk_info[vm]

    def has_disk_data(self, vm: str) -> bool:
        if vm not in self.has_data:
            self.has_data[vm] = _disk_has_data(vm)
        return self.has_data[vm]

    def media(self, vm: str) -> list[tuple[str, str]]:
        if vm not in self.live:
            self.live[vm] = _live_media(vm) if self.powered(vm) else []
        return self.live[vm]

    def peer_address(self) -> str:
        if self._peer is None:
            self._peer = next(
                (a for a, _ in self.addresses if a and a != self.shadowed), ""
            )
        return self._peer


def load_ctx() -> Ctx:
    """Gather the shared facts for one refresh."""
    ctx = Ctx(containers=podman.ps())
    ctx.addresses = host_addresses()
    ctx.shadowed = default_host_address(ctx.addresses)
    return ctx


def template_disk(vm: str) -> Path | None:
    meta = registry.load_meta(vm)
    if not meta.template_source:
        return None
    return paths.disk_path(meta.template_source)


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
        repository, _, tag = image.partition(":")
        vm_spec.image = spec.Image(repository=repository or spec.DEFAULT_REPO, tag=tag or "latest")
    progress("Allocating host ports...")
    allocated = ports.allocate_all()
    wanted = ports_map or {}
    vm_spec.network.host_web = int(wanted.get("web") or allocated["web"])
    # Only publish a port whose guest service is actually forwarded, otherwise the
    # host port forwards to a passt listener that will never answer.
    vm_spec.network.host_ssh = (
        int(wanted.get("ssh") or allocated["ssh"]) if 22 in vm_spec.network.guest_ports else 0
    )
    vm_spec.network.host_rdp = (
        int(wanted.get("rdp") or allocated["rdp"])
        if 3389 in vm_spec.network.guest_ports or wanted.get("rdp")
        else 0
    )

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
    published = [f"viewer :{vm_spec.network.host_web}"]
    if vm_spec.network.host_ssh:
        published.append(f"ssh :{vm_spec.network.host_ssh}")
    if vm_spec.network.host_rdp:
        published.append(f"rdp :{vm_spec.network.host_rdp}")
    progress(f"Created {name} ({', '.join(published)})")
    return meta


CLONE_MODES = ("linked", "full")


def clone(
    name: str,
    template: str,
    *,
    mode: str = "linked",
    cpus: int | None = None,
    ram: str | None = None,
    progress: Progress = _noop,
) -> registry.VmMeta:
    """Clone a template.

    linked  The clone's disk is a copy-on-write overlay on the template, so it is
            created instantly and costs only the blocks it writes. It shares
            storage with the template, cannot run at the same time as it, and
            must be flattened before the template can be deleted.
    full    The template's disk is converted into a standalone image. Slower and
            it copies real data, but the clone is completely independent and can
            run, move and be exported on its own.
    """
    progress = progress or _noop
    if mode not in CLONE_MODES:
        raise LifecycleError(
            f"unknown clone mode {mode!r} (expected one of {', '.join(CLONE_MODES)})"
        )
    spec.validate_name(name)
    if registry.exists(name):
        raise AlreadyExists(f"VM already exists: {name}")
    source_meta = registry.load_meta(template)
    if not source_meta.is_template:
        raise LifecycleError(f"{template} is not marked as a template")
    if podman.is_running(template):
        raise TemplateInUse(
            f"template {template} is running — stop it before cloning, "
            "its disk is locked while it is being read"
        )
    source_disk = paths.disk_path(template)
    if not disk.exists(source_disk):
        raise NotFound(f"template has no disk: {source_disk}")

    progress(f"Reading template {template}...")
    vm_spec = registry.load_spec(template)
    vm_spec.name = name
    vm_spec.blueprint = source_meta.blueprint
    vm_spec.boot.iso = ""
    if cpus is not None:
        vm_spec.resources.cpus = cpus
    if ram is not None:
        vm_spec.resources.ram = ram

    progress("Allocating host ports...")
    allocated = ports.allocate_all()
    vm_spec.network.host_web = allocated["web"]
    vm_spec.network.host_ssh = allocated["ssh"] if 22 in vm_spec.network.guest_ports else 0
    if 3389 in vm_spec.network.guest_ports:
        vm_spec.network.host_rdp = allocated["rdp"]

    clone_disk = paths.disk_path(name)
    paths.storage_dir(name).mkdir(parents=True, exist_ok=True)
    paths.disk_dir(name).mkdir(parents=True, exist_ok=True)
    if mode == "full":
        used = disk.actual_size(source_disk)
        progress(
            f"Copying the template's disk ({spec.format_size(used)} in use); "
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
        template_source=None if mode == "full" else template,
    )
    if mode == "linked":
        registry.register_dependent(name, template)
        note = "shares storage with the template"
    else:
        note = "fully independent of the template"
    progress(
        f"Cloned {template} -> {name} ({mode}, "
        f"{spec.format_size(disk.actual_size(clone_disk))} on disk, {note})"
    )
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
        raise TemplateInUse(f"{vm} is used as a template by: {', '.join(dependents)}")

    if meta.template_source:
        registry.release_dependent(vm)

    progress(f"Removing {vm}...")
    podman.rm(vm)
    from . import rdp

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


def status(vm: str, *, ctx: Ctx | None = None) -> VmStatus:
    ctx = ctx or load_ctx()
    meta = ctx.meta(vm)
    vm_spec = ctx.vm_spec(vm)
    container = ctx.state(vm)
    disk_file = paths.disk_path(vm)
    info = ctx.info(vm)
    has_disk = bool(info)
    snap_list = disk.snapshot_names(disk_file, data=info) if has_disk else []
    targets = peer_dial_targets(vm, ctx=ctx)

    result = VmStatus(
        name=vm,
        exists=True,
        container=container,
        powered=ctx.powered(vm),
        paused=container == "paused",
        cpus=vm_spec.resources.cpus,
        ram=vm_spec.resources.ram,
        disk_size=vm_spec.resources.disk,
        is_template=meta.is_template,
        template_source=meta.template_source,
        dependents=meta.dependents,
        boot_mode=vm_spec.boot.mode,
        disk_type=vm_spec.resources.disk_type,
        ports=vm_spec.network.published(),
        guest_ports=list(vm_spec.network.guest_ports),
        has_disk=has_disk,
        snapshots=snap_list,
        blueprint=meta.blueprint,
        console_url=console_url(vm, ctx=ctx),
        viewer_port=viewer_port(vm, ctx=ctx),
        kvm=vm_spec.resources.kvm,
        boot_image=boot_image(vm, ctx=ctx),
        has_disk_data=ctx.has_disk_data(vm),
        peer_reachable=bool(targets),
        peer_targets=targets,
        live_media=[label for _, label in ctx.media(vm)],
        drifted=drifted(vm, ctx=ctx),
    )
    if has_disk:
        try:
            result.disk_actual = spec.format_size(disk.actual_size(disk_file))
            result.backing = disk.backing_file(disk_file, data=info)
        except DiskError as exc:
            result.errors.append(str(exc))
    if result.drifted:
        result.notices.append(
            "vm.toml changed since this VM started; restart it to apply the change "
            f"({', '.join(result.live_media) or 'media changes'})"
        )
    if "install ISO" in result.live_media:
        where = "the RUNNING VM" if result.powered else "vm.toml"
        result.notices.append(
            f"an install ISO is attached in {where} and boots before the disk; "
            f"run: vmctl finish-install {vm}"
            + (" then restart it" if result.powered else "")
        )
    if vm_spec.media.install and "install ISO" not in result.live_media:
        result.notices.append(
            "an install ISO is attached and will boot before the disk; "
            f"run vmctl finish-install {vm} once the guest is installed"
        )
    if vm_spec.media.install and vm_spec.boot.iso:
        result.notices.append(
            f"both an attached ISO and boot.iso={vm_spec.boot.iso!r} are set — "
            f"the container would download {vm_spec.boot.iso!r} too"
        )
    if has_disk and result.has_disk_data and (paths.storage_dir(vm) / "boot.iso").is_file():
        result.notices.append(
            "a cached fallback image is still attached as the last boot device; "
            f"vmctl finish-install {vm} removes it"
        )
    if has_disk and not result.has_disk_data and not result.backing:
        result.notices.append(
            "disk is still empty, so the container fetches a 60 MB fallback image "
            "once on first start. It never boots ahead of your ISO or the disk, and "
            "it stops once the guest is installed."
        )
    return result


def console_url(vm: str, *, ctx: Ctx | None = None) -> str:
    try:
        vm_spec = ctx.vm_spec(vm) if ctx else registry.load_spec(vm)
    except Exception:
        return ""
    host_port = vm_spec.network.host_web
    if not host_port:
        return ""
    return f"http://{vm_spec.network.bind or '127.0.0.1'}:{host_port}/"


CONTAINER_MEDIA = {
    "/start.iso": "install ISO",
    "/drivers.iso": "drivers ISO",
    "/storage/boot.iso": "cached fallback",
}


def live_media(vm: str, *, ctx: Ctx | None = None) -> list[tuple[str, str]]:
    """What the running container actually has attached, read from QMP.

    The spec is not the truth while a VM is running: media, boot image and disk
    are only applied when the container is created, so an edited spec and a
    running VM legitimately disagree. Reporting the spec alone is what made a
    machine with three ISOs attached claim it "boots from disk".
    """
    if ctx is not None:
        return ctx.media(vm)
    return _live_media(vm)


def _live_media(vm: str) -> list[tuple[str, str]]:
    socket_path = paths.qmp_path(vm)
    if not socket_path.exists():
        return []
    try:
        with qmp.Qmp(socket_path, timeout=4) as client:
            files = [str(n.get("file", "")) for n in client.block_backends()]
    except (QmpError, OSError, NotFound):
        return []
    seen: list[tuple[str, str]] = []
    for item in files:
        role = CONTAINER_MEDIA.get(item)
        if role and role not in [label for _, label in seen]:
            seen.append((item, role))
    return seen


def drifted(vm: str, *, ctx: Ctx | None = None) -> bool:
    """True when the spec no longer matches the running container."""
    if ctx is None:
        ctx = load_ctx()
        if not ctx.powered(vm):
            return False
    container = ctx.container(vm)
    if container is None or not container.powered:
        return False
    try:
        meta = ctx.meta(vm)
        vm_spec = ctx.vm_spec(vm)
    except VmhubError:
        return False
    return container.labels.get(podman.LABEL_SPEC) != podman.spec_hash(
        vm_spec, meta.uuid, meta.mac, template_disk(vm)
    )


def boot_image(vm: str, *, ctx: Ctx | None = None) -> str:
    ctx = ctx or load_ctx()
    try:
        vm_spec = ctx.vm_spec(vm)
    except Exception:
        return ""
    if ctx.powered(vm):
        live = ctx.media(vm)
        if live:
            return "attached now: " + ", ".join(label for _, label in live)
        if drifted(vm, ctx=ctx):
            return "disk (running config is older than vm.toml)"
        return "disk"
    if vm_spec.media.install:
        return f"attached ISO: {Path(vm_spec.media.install).name}"
    if vm_spec.boot.iso:
        return f"downloaded on start: {vm_spec.boot.iso}"
    return "none (boots from disk)"


def disk_has_data(vm: str, *, ctx: Ctx | None = None) -> bool:
    """True once the guest has written to the disk.

    Uses qemu-img map rather than dd: the map lists which guest ranges are
    allocated, which is a cheap metadata-only query, and it works while QEMU has
    the image open. A dd-based check would need a seekable temporary output and
    silently reports an empty disk.
    """
    if ctx is not None:
        return ctx.has_disk_data(vm)
    return _disk_has_data(vm)


def _disk_has_data(vm: str) -> bool:
    return disk.has_data(paths.disk_path(vm))


def host_addresses() -> list[tuple[str, str]]:
    """(address, interface) for every global IPv4 on the host."""
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        # ip -o prints: "2: eth0    inet 10.0.0.1/24 brd ... scope global ..."
        if "inet" not in parts or len(parts) < 4:
            continue
        try:
            addr = parts[parts.index("inet") + 1].split("/")[0]
        except (ValueError, IndexError):
            continue
        iface = parts[1].rstrip(":")
        out.append((addr, iface))
    return out


def default_host_address(addresses: list[tuple[str, str]] | None = None) -> str:
    """The host address a container shadows, so it is unusable for peer traffic."""
    try:
        proc = subprocess.run(
            ["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=10
        )
        iface = ""
        for line in proc.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == "default":
                iface = parts[parts.index("dev") + 1] if "dev" in parts else ""
                break
    except Exception:
        return ""
    for addr, candidate in addresses if addresses is not None else host_addresses():
        if candidate == iface:
            return addr
    return ""


def peer_address() -> str:
    """A host address other guests can actually reach.

    The container and the guest both hold a copy of the host's default-route
    address, so that address is shadowed and unreachable from inside. Any other
    global host address works as a rendezvous point.
    """
    shadowed = default_host_address()
    for addr, _iface in host_addresses():
        if addr and addr != shadowed:
            return addr
    return ""


def peer_dial_targets(vm: str, *, ctx: Ctx | None = None) -> list[str]:
    ctx = ctx or load_ctx()
    vm_spec = ctx.vm_spec(vm)
    if vm_spec.network.bind in {"127.0.0.1", "::1", "localhost"}:
        return []
    address = ctx.peer_address()
    if not address:
        return []
    return [f"{address}:{port}" for port in sorted(vm_spec.network.published())]


def guest_info(vm: str, *, timeout: float = qga.DEFAULT_TIMEOUT) -> qga.GuestInfo:
    """Ask the running guest for its IP/hostname/OS via the agent."""
    if not podman.is_running(vm):
        return qga.GuestInfo(False, reason="VM is not running")
    return qga.query(vm, timeout=timeout)


def viewer_port(vm: str, *, ctx: Ctx | None = None) -> int:
    try:
        return (ctx.vm_spec(vm) if ctx else registry.load_spec(vm)).network.host_web
    except Exception:
        return 0


def ssh_target(vm: str) -> str:
    vm_spec = registry.load_spec(vm)
    if not vm_spec.network.host_ssh:
        return ""
    return f"{spec_user_hint(vm)}@{vm_spec.network.bind or '127.0.0.1'} -p {vm_spec.network.host_ssh}"


def spec_user_hint(vm: str) -> str:
    try:
        blueprint = blueprints.load(registry.load_spec(vm).blueprint)
    except Exception:
        return "user"
    return getattr(blueprint, "ssh_user", "user") or "user"


def list_vms() -> list[VmStatus]:
    registry.sync_all_dependents()
    ctx = load_ctx()
    out: list[VmStatus] = []
    for meta in registry.iter_metas():
        try:
            out.append(status(meta.name, ctx=ctx))
        except Exception as exc:
            broken = VmStatus(name=meta.name, exists=False)
            broken.errors.append(str(exc))
            out.append(broken)
    return out


def list_snapshots(vm: str) -> list[dict[str, Any]]:
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        return []
    return disk.snapshots(disk_file)


def take_snapshot(vm: str, name: str, *, progress: Progress = _noop) -> str:
    progress = progress or _noop
    spec.validate_name(name)
    if not podman.is_running(vm):
        raise VmNotRunning(f"{vm} is not running — start it before snapshotting")
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

    progress(f"Taking snapshot {name} on {vm}...")
    with qmp.Qmp(paths.qmp_path(vm)) as client:
        if not client.is_running():
            raise VmNotRunning(f"{vm} guest is not running (QMP status not 'running')")
        device = qmp.primary_block_device(client)
        client.take_snapshot(device, name)
    progress(f"Snapshot {name} created on {vm}")
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
    progress(f"Deleted snapshot {name}")


def revert(vm: str, name: str, *, progress: Progress = _noop, restart: bool = True) -> None:
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

    for stale in newer:
        progress(f"Discarding newer snapshot {stale}...")
        try:
            disk.delete_snapshot(disk_file, stale)
        except (NotFound, DiskError) as exc:
            progress(f"  could not discard {stale}: {exc}")

    progress(f"{vm} reverted to {name}")
    if newer:
        progress(f"Discarded {len(newer)} snapshot(s) taken after {name}")
    if restart and was_running:
        start(vm, progress=progress)


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


def os_replace(src: Path, dst: Path) -> None:
    import os

    os.replace(src, dst)


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
