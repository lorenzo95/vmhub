"""The commands that are about the published host ports."""

from __future__ import annotations

import argparse
import time

from .. import podman, ports, rdp, registry, spec
from ..errors import VmhubError
from .output import Out


def _port_by_target(net: spec.Network, target: str) -> spec.Port | None:
    """Find a port entry by its role or by the host port it publishes."""
    for port in net.ports:
        if port.role == target or str(port.host) == target:
            return port
    return None


def port_scope(port: spec.Port, network_bind: str) -> str:
    """How far one port reaches, in the same words the ports table uses.

    This used to print bind= only when the port had a bind of its own,
    so a port inheriting a network-wide 0.0.0.0 was listed exactly like
    a loopback one: the one case you most want to notice was the one
    case the list stayed quiet about.
    """
    if not port.host:
        return ""
    effective = spec.effective_bind(port, network_bind)
    if spec.is_exposed(effective):
        return f"  bind={effective}  exposed to the network"
    return f"  bind={effective}  localhost only"


def cmd_ports(args: argparse.Namespace, out: Out) -> int:
    action = getattr(args, "ports_action", "list")
    vm_spec = registry.load_spec(args.name)
    net = vm_spec.network

    def restarted_note() -> None:
        if podman.is_running(args.name):
            out.say(out.dim("  takes effect the next time the VM starts"))

    if action == "list":
        if not net.ports:
            out.say(out.dim(f"{args.name} publishes no ports"))
            return 0
        claimed = ports.used_by_vms(args.name)
        mine = ports.leftover_ports(args.name, vm_spec)
        own = ports.own_forwarder_ports(args.name, vm_spec)
        for port in net.ports:
            if not port.host:
                note = out.dim("not published")
            elif port.host in claimed:
                note = out.bad(f"claimed by {claimed[port.host]}")
            elif not ports.port_is_free(port.host):
                if port.host in mine:
                    note = out.bad("held by a leftover instance of this VM")
                elif port.host in own:
                    note = out.dim("held by this VM")
                else:
                    note = out.bad("bound by another process")
            else:
                note = out.good("free")
            role = f"  [{port.role}]" if port.role else ""
            scope_text = port_scope(port, net.bind)
            bind = (
                out.warn(scope_text)
                if "exposed to the network" in scope_text
                else out.dim(scope_text)
            )
            left = f"{port.host if port.host else '-':>5} -> {port.guest:<6}"
            out.say(f"{left}  {port.label}{role}{bind}  {note}")
        forward = ", ".join(str(p) for p in sorted(net.guest_ports)) or "none"
        out.say(out.dim(f"  guest ports forwarded to the host: {forward}"))
        return 0

    if action == "add":
        role = args.role or ""
        guest = args.guest or ports.ROLE_GUEST.get(role, 0)
        if not guest:
            print(out.bad("give a guest port, or use --as web|ssh|rdp"))
            return 2
        if role and net.by_role(role) is not None:
            print(out.bad(
                f"{args.name} already publishes a {role} port; "
                f"move it with: vmctl ports set {args.name} {role} --host PORT"
            ))
            return 1
        if args.host:
            if args.host in net.published():
                print(out.bad(f"host port {args.host} is already published by this VM"))
                return 1
            host = args.host
        else:
            avoid = set(ports.used_by_vms(args.name)) | set(net.published())
            host = (
                ports.allocate(role, also_wanted=avoid)
                if role
                else ports.allocate_any(also_wanted=avoid)
            )
        bind = getattr(args, "bind", "") or ""
        net.ports.append(spec.Port(guest=guest, host=host, role=role, name=args.label, bind=bind))
        # A guest service the container is not forwarding never answers, so
        # turn the forwarding on now rather than leaving a port that only
        # looks live. The web console is the container's own listener and has
        # no guest side to forward.
        if role != "web" and guest not in net.guest_ports:
            net.guest_ports = sorted({*net.guest_ports, guest})
            out.say(out.dim(f"  added guest {guest} to network.guest_ports"))
        try:
            vm_spec.validate()
        except VmhubError as exc:
            print(out.bad(str(exc)))
            return 1
        registry.save_spec(vm_spec)
        out.say(out.good(f"published guest {guest} on host {host}"))
        restarted_note()
        return 0

    port = _port_by_target(net, args.target)
    if port is None:
        print(out.bad(f"{args.name} publishes no port {args.target!r}"))
        return 1

    if action == "rm":
        net.ports.remove(port)
        ports.prune_guest_forward(net, port.guest)
        vm_spec.validate()
        registry.save_spec(vm_spec)
        out.say(out.good(f"stopped publishing {port.label}"))
        restarted_note()
        return 0

    # action == "set"
    if not args.host and not args.guest and getattr(args, "bind", None) is None:
        print(out.bad("give the new host port with --host, the guest port with --guest, or a per-port --bind"))
        return 2
    new_host = int(args.host) if args.host else port.host
    new_guest = int(args.guest) if args.guest else port.guest
    new_bind = getattr(args, "bind", None)
    if (new_host == port.host and new_guest == port.guest
            and (new_bind is None or new_bind == port.bind)):
        out.say(out.dim(f"{port.label} is already host {port.host} to guest {port.guest}"
            + (f" bind={port.bind}" if port.bind else "")))
        return 0
    if new_bind is not None:
        port.bind = new_bind
    if ports.host_taken_by_other(net, new_host, exclude=port):
        print(out.bad(f"host port {new_host} is already published by this VM"))
        return 1
    old_guest = port.guest
    port.host = new_host
    port.guest = new_guest
    if port.role != "web" and new_guest not in net.guest_ports:
        net.guest_ports = sorted({*net.guest_ports, new_guest})
    if old_guest != new_guest:
        # The forwarding this port switched on for its old guest port has
        # to go back off, or the VM is left forwarding a port nothing uses.
        ports.prune_guest_forward(net, old_guest)
    vm_spec.validate()
    registry.save_spec(vm_spec)
    if new_guest == old_guest:
        out.say(out.good(f"{port.label} now on host {new_host}"))
    else:
        out.say(out.good(f"{port.label} now guest {new_guest} on host {new_host}"))
    restarted_note()
    return 0


def cmd_repair_ports(args: argparse.Namespace, out: Out) -> int:
    clashes = ports.collisions()
    if not clashes:
        out.say(out.good("no port conflicts"))
        return 0
    for port, names in sorted(clashes.items()):
        out.say(out.warn(f"  port {port} claimed by {', '.join(names)}"))
    if args.dry_run:
        return 0
    for vm, changes in ports.repair_all().items():
        out.say(f"  {vm}:")
        for change in changes:
            out.say(out.good(f"    {change}"))
    if not ports.collisions():
        out.say(out.good("all port conflicts resolved"))
    return 0
