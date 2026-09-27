from __future__ import annotations

import socket
from typing import Iterable

from . import registry, spec
from .errors import NoFreePort

BASES = {"web": 8006, "ssh": 2222, "rdp": 3389}
MAX_OFFSET = 4000
PROBE_HOST = "127.0.0.1"


def port_is_free(port: int, host: str = PROBE_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def used_by_vms(exclude: str | None = None) -> dict[str, int]:
    used: dict[str, int] = {}
    for meta in registry.iter_metas():
        if exclude and meta.name == exclude:
            continue
        try:
            vm_spec = registry.load_spec(meta.name)
        except Exception:
            continue
        for host_port in vm_spec.network.published():
            used[f"{host_port}"] = meta.name
    return used


def allocate(kind: str, *, exclude: str | None = None, also_wanted: Iterable[int] = ()) -> int:
    base = BASES[kind]
    claimed = set(int(p) for p in also_wanted)
    for offset in range(0, MAX_OFFSET):
        candidate = base + offset
        if candidate > 65535:
            break
        if candidate in claimed:
            continue
        if port_is_free(candidate):
            return candidate
    raise NoFreePort(f"no free {kind} port available from base {base}")


def allocate_all(*, exclude: str | None = None) -> dict[str, int]:
    used = set(int(p) for p in used_by_vms(exclude).keys())
    assigned: dict[str, int] = {}
    for kind in ("web", "ssh", "rdp"):
        port = allocate(kind, exclude=exclude, also_wanted=used | set(assigned.values()))
        assigned[kind] = port
        used.add(port)
    return assigned


def verify(vm_spec: spec.VmSpec, *, check_busy: bool = True) -> list[str]:
    warnings: list[str] = []
    guest_mapped = {
        vm_spec.network.host_ssh: 22,
        vm_spec.network.host_rdp: 3389,
    }
    for host_port, guest_port in vm_spec.network.published().items():
        if guest_mapped.get(host_port) != guest_port:
            continue
        if guest_port not in vm_spec.network.guest_ports:
            warnings.append(
                f"host port {host_port} forwards guest {guest_port} but {guest_port} "
                f"is not in network.guest_ports — add it or the guest service is unreachable"
            )
    if check_busy:
        for host_port in vm_spec.network.published():
            if not port_is_free(host_port):
                warnings.append(
                    f"host port {host_port} is already bound by another process"
                )
    return warnings


def collisions() -> dict[int, list[str]]:
    """Host ports claimed by more than one VM."""
    owners: dict[int, list[str]] = {}
    for meta in registry.iter_metas():
        try:
            vm_spec = registry.load_spec(meta.name)
        except Exception:
            continue
        for host_port in vm_spec.network.published():
            owners.setdefault(host_port, []).append(meta.name)
    return {port: sorted(names) for port, names in owners.items() if len(names) > 1}


# host_web publishes the container's own noVNC server; it is never a guest
# service and must never be gated on guest_ports.
PORT_KINDS = {
    "host_web": ("web", None),
    "host_ssh": ("ssh", 22),
    "host_rdp": ("rdp", 3389),
}


def _claimed_by_others(exclude: str) -> dict[int, str]:
    claimed: dict[int, str] = {}
    for meta in registry.iter_metas():
        if meta.name == exclude:
            continue
        try:
            vm_spec = registry.load_spec(meta.name)
        except Exception:
            continue
        for port in vm_spec.network.published():
            claimed.setdefault(port, meta.name)
    return claimed


def ensure_for(vm: str) -> list[str]:
    """Make this VM's host ports usable, reassigning the ones that are not.

    A port is unusable if another VM claims it, or if some other process holds
    it. Ports whose guest service is not forwarded at all are dropped rather
    than reassigned, since nothing would answer them either way.
    """
    vm_spec = registry.load_spec(vm)
    claimed = _claimed_by_others(vm)
    changes: list[str] = []
    for attr, (kind, guest) in PORT_KINDS.items():
        current = getattr(vm_spec.network, attr)
        if not current:
            continue
        if guest is not None and guest not in vm_spec.network.guest_ports:
            setattr(vm_spec.network, attr, 0)
            changes.append(
                f"cleared {attr}={current} (guest {guest} is not forwarded)"
            )
            continue
        holder = claimed.get(current)
        if holder is None and port_is_free(current):
            continue
        avoid = set(claimed) | {
            p for p in vm_spec.network.published()
        } | {current}
        fresh = allocate(kind, exclude=vm, also_wanted=avoid)
        setattr(vm_spec.network, attr, fresh)
        why = f"claimed by {holder}" if holder else "bound by another process"
        changes.append(f"{attr} {current} -> {fresh} ({why})")
    if changes:
        registry.save_spec(vm_spec)
    return changes


def repair_all() -> dict[str, list[str]]:
    """Ensure every VM's ports, earliest VM keeping what it already has."""
    fixed: dict[str, list[str]] = {}
    for name in sorted(registry.all_names()):
        changes = ensure_for(name)
        if changes:
            fixed[name] = changes
    return fixed
