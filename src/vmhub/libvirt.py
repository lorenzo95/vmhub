from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import disk, paths, ports, registry, spec
from .errors import LifecycleError, NotFound, VmhubError

VIRSH = "virsh"
DEFAULT_URI = "qemu:///system"
KIB = 1024


@dataclass
class Domain:
    name: str
    state: str = ""
    memory_kib: int = 0
    vcpus: int = 0
    disks: list[str] = field(default_factory=list)

    @property
    def ram(self) -> str:
        return spec.format_size(self.memory_kib * KIB) if self.memory_kib else "4G"


def available() -> bool:
    return shutil.which(VIRSH) is not None


def _virsh(args: list[str], *, uri: str = DEFAULT_URI, check: bool = True) -> str:
    if not available():
        raise NotFound(f"{VIRSH} is not installed")
    proc = subprocess.run(
        [VIRSH, "-c", uri, *args], capture_output=True, text=True, timeout=60
    )
    noise = f"{proc.stderr or ''}{proc.stdout or ''}"
    # polkit may answer a read-only query once and then refuse; the refusal must
    # not look like "there are no domains".
    refused = "authentication" in noise.lower() or "failed to connect to the hypervisor" in noise.lower()
    if refused and not proc.stdout.strip():
        raise VmhubError(
            f"libvirt refused the connection to {uri}. Run "
            f"`virsh -c {uri} list --all` once in an interactive session to "
            f"authorise it, then retry."
        )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise VmhubError(f"virsh {' '.join(args[:2])} failed: {detail or 'no output'}")
    return proc.stdout


def names(*, uri: str = DEFAULT_URI) -> list[str]:
    out = _virsh(["list", "--all", "--name"], uri=uri, check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def describe(name: str, *, uri: str = DEFAULT_URI) -> Domain:
    xml = _virsh(["dumpxml", name], uri=uri)
    state = _virsh(["domstate", name], uri=uri, check=False).strip()
    return parse_domain(xml, name=name, state=state)


def parse_domain(xml: str, *, name: str = "", state: str = "unknown") -> Domain:
    """Extract what vmhub needs from libvirt's XML. Pure, so it can be tested."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise VmhubError(f"could not parse the libvirt XML for {name}: {exc}") from exc

    domain = Domain(name=root.findtext("name") or name)
    domain.state = state or "unknown"

    memory = root.find("memory")
    if memory is not None and memory.text:
        try:
            domain.memory_kib = int(memory.text)
        except ValueError:
            pass

    vcpu = root.findtext("vcpu")
    if vcpu:
        try:
            domain.vcpus = int(vcpu.strip())
        except ValueError:
            pass

    for node in root.findall("./devices/disk"):
        if node.get("device") != "disk":
            continue
        source = node.find("source")
        if source is None:
            continue
        path = source.get("file") or source.get("dev")
        if path:
            domain.disks.append(path)
    return domain


def import_domain(
    name: str,
    *,
    new_name: str | None = None,
    uri: str = DEFAULT_URI,
    disk_type: str = "ide",
    source_index: int = 0,
    move: bool = False,
    progress=None,
) -> str:
    """Bring a libvirt domain's disk under vmhub as a new VM.

    The disk is converted into a fresh qcow2 rather than registered in place: the
    libvirt domain keeps working, and vmhub gets an image it owns. `disk_type`
    defaults to ide because a guest installed against IDE drivers (typically
    Windows) will not boot on virtio-scsi.
    """
    domain = describe(name, uri=uri)
    return materialize(
        domain,
        new_name=new_name,
        disk_type=disk_type,
        source_index=source_index,
        move=move,
        progress=progress,
    )


def materialize(
    domain: Domain,
    *,
    new_name: str | None = None,
    disk_type: str = "ide",
    source_index: int = 0,
    move: bool = False,
    progress=None,
) -> str:
    """Create a vmhub VM from an already-described domain.

    Split out from import_domain so the conversion can be exercised without a
    reachable libvirt, which polkit may gate behind an interactive prompt.
    """
    progress = progress or (lambda _m: None)
    target = spec.validate_name(new_name or domain.name)
    if registry.exists(target):
        raise VmhubError(f"VM already exists: {target}")
    name = domain.name
    if "shut" not in domain.state.lower() and "off" not in domain.state.lower():
        raise LifecycleError(
            f"{name} is {domain.state}; shut the libvirt domain down before importing "
            f"it, so its disk is consistent"
        )
    if not domain.disks:
        raise NotFound(f"{name} has no disk device to import")
    if source_index >= len(domain.disks):
        raise NotFound(
            f"{name} has {len(domain.disks)} disk(s); --disk-index {source_index} "
            f"is out of range"
        )

    source = Path(domain.disks[source_index])
    if not source.is_file():
        raise NotFound(f"{name}'s disk is not a regular file: {source}")
    progress(f"{name}: {domain.ram} ram, {domain.vcpus or 2} vcpu, disk {source}")
    paths.disk_dir(target).mkdir(parents=True, exist_ok=True)
    paths.storage_dir(target).mkdir(parents=True, exist_ok=True)
    destination = paths.disk_path(target)

    progress(f"Converting {source.name} into a qcow2 vmhub owns...")
    disk.convert(source, destination, "qcow2", compress=False)

    vm_spec = spec.VmSpec(name=target)
    vm_spec.resources.cpus = max(1, domain.vcpus or 2)
    vm_spec.resources.ram = domain.ram
    vm_spec.resources.disk_type = disk_type
    vm_spec.resources.disk = spec.format_size(disk.virtual_size(destination))
    # Firmware is detected from the imported disk by the container.
    vm_spec.boot.mode = "auto"
    vm_spec.network.guest_ports = [22]
    vm_spec.network.host_web = ports.allocate("web")
    vm_spec.network.host_ssh = ports.allocate("ssh")
    vm_spec.validate()

    registry.save_spec(vm_spec)
    registry.create_meta(target, blueprint="", image=vm_spec.image.ref, notes=f"imported from libvirt domain {name}")

    if move:
        progress(f"Removing the original because --move was given: {source}")
        try:
            source.unlink()
        except OSError as exc:
            progress(f"  could not remove {source}: {exc}")

    progress(
        f"Imported {name} as {target} ({vm_spec.resources.disk}, {vm_spec.resources.ram}). "
        f"Disk bus is {disk_type}; switch to scsi once the guest has virtio drivers."
    )
    return target
