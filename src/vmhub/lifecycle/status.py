"""
status: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

import subprocess

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any

from .. import blueprints, disk, paths, podman, ports, procs, qga, qmp, registry, spec

from ..errors import (
    DiskError,
    NotFound,
    QmpError,
    VmhubError,
)

from .progress import Progress, _noop


class Notice(str):
    """A status note, and the action that resolves it where one exists.

    Subclasses str so it keeps printing and comparing exactly as the plain
    strings it replaces -- the CLI prints these and the tests match on
    substrings -- while carrying what the GUI can offer inline.
    """

    action: str

    def __new__(cls, text: str, action: str = "") -> "Notice":
        obj = super().__new__(cls, text)
        obj.action = action
        return obj


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
    finish_install_needed: bool = False
    notices: list[Notice] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # What is still running for a VM whose container podman has lost.
    leftover: bool = False
    leftover_pids: list[int] = field(default_factory=list)
    leftover_ports: list[int] = field(default_factory=list)

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
    _procs: procs.Snapshot | None = None

    def container(self, vm: str) -> podman.Container | None:
        return self.containers.get(vm)

    def state(self, vm: str) -> str:
        container = self.containers.get(vm)
        return container.state if container else "absent"

    def container_powered(self, vm: str) -> bool:
        """Whether podman still has a running container for this VM."""
        container = self.containers.get(vm)
        return bool(container and container.powered)

    def powered(self, vm: str) -> bool:
        """Whether the VM is running, container or not.

        A VM whose container podman has lost is still running: its QEMU
        is alive and holding the disk open. Reporting it as stopped is
        what lets the leftovers sit there for ever.
        """
        if self.container_powered(vm):
            return True
        return self.leftovers(vm).qemu is not None

    def proc_snapshot(self) -> procs.Snapshot:
        """The host's QEMU, forwarder and conmon processes, read once."""
        if self._procs is None:
            self._procs = procs.scan()
        return self._procs

    def leftovers(self, vm: str) -> procs.Leftovers:
        """What is still running for a VM that podman has no container for."""
        meta = self.meta(vm)
        try:
            vm_spec = self.vm_spec(vm)
        except Exception:
            vm_spec = None
        published = vm_spec.network.published() if vm_spec else ()
        return procs.leftovers(
            self.proc_snapshot(),
            uuid=meta.uuid,
            container=podman.container_name(vm),
            host_ports=published,
        )

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
    # Only worth looking for when podman has no running container: a VM
    # with a container is managed by podman, and anything else it left
    # behind is a stale process from an earlier life, not a live VM.
    leftover = procs.Leftovers() if ctx.container_powered(vm) else ctx.leftovers(vm)

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
        leftover=leftover.running,
        leftover_pids=leftover.pids,
        leftover_ports=leftover.held_ports,
    )
    if leftover.running:
        held = ", ".join(str(port) for port in sorted(leftover.held_ports))
        result.notices.append(
            f"podman lost the container for {vm}, but {leftover.describe()} is still "
            f"running -- the VM is up"
            + (f" and still holding host port {held}" if held else "")
            + f", but vmhub cannot manage it. Clear it with: vmctl stop {vm}"
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
        result.notices.append(Notice(
            f"an install ISO is attached in {where} and boots before the disk; "
            f"run: vmctl finish-install {vm}"
            + (" then restart it" if result.powered else ""),
            action="finish-install",
        ))
    if vm_spec.media.install and "install ISO" not in result.live_media:
        result.notices.append(Notice(
            "an install ISO is attached and will boot before the disk; "
            f"run vmctl finish-install {vm} once the guest is installed",
            action="finish-install",
        ))
    if vm_spec.media.install and vm_spec.boot.iso:
        result.notices.append(
            f"both an attached ISO and boot.iso={vm_spec.boot.iso!r} are set — "
            f"the container would download {vm_spec.boot.iso!r} too"
        )
    cached_fallback = has_disk and result.has_disk_data and (
        paths.storage_dir(vm) / "boot.iso"
    ).is_file()
    if cached_fallback:
        result.notices.append(Notice(
            "a cached fallback image is still attached as the last boot device; "
            f"finish-install it below (or vmctl finish-install {vm}) to remove it",
            action="finish-install",
        ))
    # Same conditions as the notices above, so the GUI button and the
    # advice never disagree. A fresh alpine VM sets boot.iso on purpose
    # and has nothing to detach, so it is deliberately not offered.
    result.finish_install_needed = bool(vm_spec.media.install) or cached_fallback
    if has_disk and not result.has_disk_data and not result.backing:
        result.notices.append(
            "disk is still empty, so the container downloads an Alpine Linux ISO "
            "(~60 MB) on first start and attaches it as a boot device. With "
            "nothing else to boot, it boots that; if an ISO of yours is attached but "
            "the firmware does not take it, Alpine is where you land. Set "
            "boot.iso, or run vmctl finish-install once the guest is installed, and "
            "it stops being used."
        )
    return result


def console_url(vm: str, *, ctx: Ctx | None = None) -> str:
    try:
        vm_spec = ctx.vm_spec(vm) if ctx else registry.load_spec(vm)
    except Exception:
        return ""
    host_port = vm_spec.network.host_for("web")
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
        return (ctx.vm_spec(vm) if ctx else registry.load_spec(vm)).network.host_for("web")
    except Exception:
        return 0


def ssh_target(vm: str) -> str:
    vm_spec = registry.load_spec(vm)
    host_ssh = vm_spec.network.host_for("ssh")
    if not host_ssh:
        return ""
    return f"{spec_user_hint(vm)}@{vm_spec.network.bind or '127.0.0.1'} -p {host_ssh}"


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
