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


def used_by_vms(exclude: str | None = None) -> dict[str, str]:
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
    forwarded = {int(p) for p in vm_spec.network.guest_ports}
    for port in vm_spec.network.ports:
        if not port.host:
            continue
        # The web console is the container's own listener, not a guest
        # service, so it is never gated on network.guest_ports.
        if port.role != "web" and port.guest not in forwarded:
            warnings.append(
                f"host port {port.host} forwards guest {port.guest} but {port.guest} "
                f"is not in network.guest_ports — add it or the guest service is unreachable"
            )
        if check_busy and not port_is_free(port.host):
            warnings.append(
                f"host port {port.host} is already bound by another process"
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


# The guest port behind each role. web is the container's own noVNC server,
# which is why it is the one role that is not a guest service.
ROLE_GUEST = {"web": 8006, "ssh": 22, "rdp": 3389}


def allocate_any(*, also_wanted: Iterable[int] = (), start: int = 9000) -> int:
    """A free host port for a port entry that has no role of its own."""
    claimed = set(int(p) for p in also_wanted)
    for candidate in range(start, min(start + MAX_OFFSET, 65536)):
        if candidate in claimed:
            continue
        if port_is_free(candidate):
            return candidate
    raise NoFreePort(f"no free host port available from {start}")


def defaults(guest_ports: Iterable[int]) -> list["spec.Port"]:
    """The entries a VM gets when it has no port list of its own."""
    forwarded = {int(p) for p in guest_ports}
    out = [spec.Port(guest=8006, host=0, role="web")]
    if 22 in forwarded:
        out.append(spec.Port(guest=22, host=0, role="ssh"))
    if 3389 in forwarded:
        out.append(spec.Port(guest=3389, host=0, role="rdp"))
    return out


def host_taken_by_other(
    net: "spec.Network", host: int, *, exclude: "spec.Port | None" = None
) -> bool:
    """Whether some entry other than `exclude` already publishes this host port.

    The entry being edited counts as not taken. Comparing against the guest
    number already on that host instead would make an edit look like a
    collision with itself whenever the guest number changed.
    """
    return any(port.host == host and port is not exclude for port in net.ports)


def prune_guest_forward(net: "spec.Network", guest: int) -> None:
    """Stop forwarding a guest port that nothing publishes any more.

    Adding a forward also turns the container's forwarding on for that guest
    port, so taking the forward away has to take both back. Otherwise the
    VM is left telling the container to forward a port that no longer has
    anything on the other side, and the UI still lists it as forwarded.
    """
    if any(port.guest == guest for port in net.ports):
        return
    if guest in net.guest_ports:
        net.guest_ports = [p for p in net.guest_ports if int(p) != guest]


def reallocate_published(entries: Iterable["spec.Port"]) -> list["spec.Port"]:
    """Give every published entry a host port that is free on this machine.

    For a spec copied onto the same machine -- a clone -- where the numbers on
    it belong to the copy's source and would collide with it. Roles keep their
    familiar base range via allocate(); the user's own forwards move to the
    high range. An entry with host=0 was deliberately unpublished and stays
    that way, which reallocate() above does not do.
    """
    claimed = set(int(p) for p in used_by_vms())
    out: list[spec.Port] = []
    for entry in entries:
        if not entry.host:
            out.append(entry)
            continue
        host = allocate(entry.role, also_wanted=claimed) if entry.role \
            else allocate_any(also_wanted=claimed)
        claimed.add(host)
        out.append(
            spec.Port(
                guest=entry.guest, host=host, role=entry.role, name=entry.name
            )
        )
    return out


def reallocate(entries: Iterable["spec.Port"]) -> list["spec.Port"]:
    """Give every entry a host port on this machine.

    Used for specs that arrive from elsewhere -- an export bundle, an import
    from another host -- where the numbers they name belong to a different
    machine. Roles keep their familiar base range; anything else is given a
    port from the high range. The caller's list is not modified.
    """
    claimed = set(int(p) for p in used_by_vms())
    out: list[spec.Port] = []
    for entry in entries:
        host = allocate(entry.role, also_wanted=claimed) if entry.role else allocate_any(also_wanted=claimed)
        claimed.add(host)
        out.append(spec.Port(guest=entry.guest, host=host, role=entry.role, name=entry.name))
    return out


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


def conflicts_for(vm: str) -> list[str]:
    """Which of this VM's host ports cannot be used, and why. Never writes.

    Kept separate from ensure_for() so start() can refuse instead of quietly
    picking a different number than the one in vm.toml. A port the user chose
    is a decision; overwriting it on the way to starting is how you get told
    your VM runs an image/port you did not ask for.
    """
    vm_spec = registry.load_spec(vm)
    claimed = _claimed_by_others(vm)
    problems: list[str] = []
    for port in vm_spec.network.ports:
        current = port.host
        if not current:
            continue
        holder = claimed.get(current)
        if holder is not None:
            problems.append(
                f"{port.label} host port {current} is claimed by VM {holder!r}"
            )
        elif not port_is_free(current):
            problems.append(
                f"{port.label} host port {current} is bound by another process"
            )
    return problems


def ensure_for(vm: str) -> list[str]:
    """Make this VM's host ports usable, reassigning the ones that are not.

    A port is unusable if another VM claims it, or if some other process holds
    it. Ports whose guest service is not forwarded at all are dropped rather
    than reassigned, since nothing would answer them either way.
    """
    vm_spec = registry.load_spec(vm)
    claimed = _claimed_by_others(vm)
    forwarded = {int(p) for p in vm_spec.network.guest_ports}
    changes: list[str] = []
    for port in vm_spec.network.ports:
        current = port.host
        if not current:
            continue
        if port.role != "web" and port.guest not in forwarded:
            port.host = 0
            changes.append(f"cleared {port.label} (guest {port.guest} is not forwarded)")
            continue
        holder = claimed.get(current)
        if holder is None and port_is_free(current):
            continue
        avoid = set(claimed) | set(vm_spec.network.published()) | {current}
        fresh = allocate(port.role, also_wanted=avoid) if port.role else allocate_any(also_wanted=avoid)
        port.host = fresh
        why = f"claimed by {holder}" if holder else "bound by another process"
        changes.append(f"{port.label} {current} -> {fresh} ({why})")
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
