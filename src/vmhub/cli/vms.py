"""The commands that are about the VMs themselves."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from .. import blueprints, disk, lifecycle, paths, podman, ports, rdp, registry, spec
from ..errors import VmhubError
from .output import Out, confirm, open_url


STATE_COLOURS = {"running": "good", "paused": "warn", "stopped": "dim", "exited": "bad"}


def cmd_ls(args: argparse.Namespace, out: Out) -> int:
    statuses = lifecycle.list_vms()
    if args.quiet:
        for item in statuses:
            print(item.name)
        return 0
    if not statuses:
        out.say(out.dim("No VMs yet. Create one with: vmctl new <name> --blueprint debian13"))
        return 0

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": s.name,
                        "state": s.summary,
                        "template": s.is_template,
                        "template_source": s.template_source,
                        "leftover": s.leftover,
                        "ports": s.ports,
                        "cpus": s.cpus,
                        "ram": s.ram,
                        "disk": s.disk_size,
                        "snapshots": s.snapshots,
                        "console": s.console_url,
                    }
                    for s in statuses
                ],
                indent=2,
            )
        )
        return 0

    width = max(len(s.name) for s in statuses)
    out.say(
        out.bold(
            f"{'NAME'.ljust(width)}  {'STATE':8s}  {'RAM':>5s} {'CPU':>3s}  "
            f"{'DISK':>6s}  {'USED':>6s}  PORTS"
        )
    )
    for item in statuses:
        paint = getattr(out, STATE_COLOURS.get(item.summary, "dim"))
        tags = []
        if item.is_template:
            tags.append(out.info("template"))
        if item.leftover:
            tags.append(out.warn("leftover"))
        if item.template_source:
            tags.append(out.dim(f"clone of {item.template_source}"))
        if item.peer_targets:
            tags.append(out.dim(f"peer {item.peer_targets[0]}"))
        if item.snapshots:
            tags.append(out.dim(f"{len(item.snapshots)} snap"))
        port_text = ",".join(str(p) for p in sorted(item.ports)) or "-"
        out.say(
            f"{item.name.ljust(width)}  {paint(item.summary.ljust(8))}  "
            f"{item.ram:>5s} {item.cpus:>3d}  {item.disk_size:>6s}  "
            f"{(item.disk_actual or '-'):>6s}  {port_text}"
            + (f"  {' '.join(tags)}" if tags else "")
        )
        for problem in item.errors:
            out.say(out.bad(f"    {problem}"))
        for note in item.notices:
            out.say(out.warn(f"    note: {note}"))
    return 0


def cmd_info(args: argparse.Namespace, out: Out) -> int:
    item = lifecycle.status(args.name)
    meta = registry.load_meta(args.name)
    vm_spec = registry.load_spec(args.name)
    out.say(out.bold(f"{item.name}"))
    # Which published ports are reachable from off this machine. A port
    # with no bind of its own inherits network.bind, so this has to be
    # worked out per port rather than read off the network default.
    exposed_hosts = {
        port.host
        for port in vm_spec.network.ports
        if port.host
        and spec.is_exposed(spec.effective_bind(port, vm_spec.network.bind))
    }
    rows = [
        ("state", item.summary),
        ("blueprint", item.blueprint or "-"),
        ("image", vm_spec.image.ref),
        ("uuid", meta.uuid),
        ("mac", meta.mac),
        ("cpus / ram", f"{item.cpus} / {item.ram}"),
        ("disk", f"{item.disk_size} virtual, {item.disk_actual or '?'} used"),
        ("disk type", item.disk_type),
        ("boot mode", item.boot_mode),
        ("backing file", item.backing or "none (self-contained)"),
        ("template", "yes" if item.is_template else "no"),
        ("clone of", item.template_source or "-"),
        ("dependents", ", ".join(item.dependents) or "-"),
        ("console", item.console_url or "-"),
        ("host ports", ", ".join(
            f"{h}->{g}" + ("  [exposed]" if h in exposed_hosts else "")
            for h, g in sorted(item.ports.items())
        ) or "-"),
        ("guest ports", ", ".join(str(p) for p in item.guest_ports) or "-"),
        ("snapshots", ", ".join(item.snapshots) or "-"),
        ("boot media", item.boot_image or "-"),
        ("internet", "allowed" if vm_spec.network.internet else "blocked (--internal)"),
        ("peer dial", ", ".join(item.peer_targets)
            if item.peer_targets
            else (
                f"{len(exposed_hosts)} port(s) reachable from the network; "
                "the rest are loopback only"
                if exposed_hosts
                else "loopback only (set network.bind=0.0.0.0 to expose, "
                "or a per-port --bind)"
            )),
        ("shutdown", f"timeout {vm_spec.shutdown.timeout}s"
            + (", no ACPI" if vm_spec.shutdown.skip_acpi else ", ACPI")),
        ("disk has data", "yes" if item.has_disk_data else "no (not installed yet)"),
        ("disk path", str(paths.disk_path(args.name))),
    ]
    for note in item.notices:
        out.say(f"  {'':16s} {out.warn(note)}")
    for label, value in rows:
        out.say(f"  {label:16s} {value}")
    return 0


def cmd_new(args: argparse.Namespace, out: Out) -> int:
    if args.blueprint and not args.no_blueprint:
        try:
            blueprints.load(args.blueprint)
        except VmhubError as exc:
            print(out.bad(str(exc)))
            return 2
    lifecycle.create(
        args.name,
        blueprint=args.blueprint,
        cpus=args.cpus,
        ram=args.ram,
        disk_size=args.disk,
        disk_type=args.disk_type,
        boot_mode=args.boot_mode,
        boot_iso=args.iso,
        image=args.image,
        progress=out.step,
    )
    if not args.no_start:
        lifecycle.start(args.name, progress=out.step)
        url = lifecycle.console_url(args.name)
        if url:
            out.say()
            out.say(f"Console: {out.info(url)}")
    if args.mark_template:
        lifecycle.stop(args.name, progress=out.step)
        lifecycle.mark_template(args.name, progress=out.step)
    return 0


def error_text(exc: BaseException) -> str:
    """An error, with the advice the CLI gives for it.

    An error that knows how it can be fixed carries that advice apart
    from its message, so the GUI can offer a button instead of a
    command and this can print the command.
    """
    hint = getattr(exc, "hint", "")
    return f"{exc}\n{hint}" if hint else str(exc)


def cmd_start(args: argparse.Namespace, out: Out) -> int:
    failed = 0
    for name in args.names:
        try:
            lifecycle.start(
                name,
                progress=None if args.quiet else out.step,
                repair=args.repair_ports,
            )
            if not args.quiet:
                out.say(out.good(f"{name} started"))
        except VmhubError as exc:
            failed += 1
            out.say(out.bad(f"{name}: {error_text(exc)}"))
    if args.console and not failed:
        open_url(lifecycle.console_url(args.names[0]))
    return 1 if failed else 0


def cmd_stop(args: argparse.Namespace, out: Out) -> int:
    mode = args.mode or ("force" if args.force else None)
    names = args.names or [s.name for s in lifecycle.list_vms() if s.powered]
    if not names:
        out.say(out.dim("nothing running"))
        return 0
    failed = 0
    for name in names:
        try:
            used = lifecycle.stop(
                name, mode=mode, progress=None if args.quiet else out.step
            )
            if not args.quiet:
                out.say(out.good(f"{name} stopped ({used})"))
        except VmhubError as exc:
            failed += 1
            out.say(out.bad(f"{name}: {exc}"))
    # A VM that did not stop is still running, and `vmctl stop x && vmctl rm
    # x` must not delete it. Reporting the failure in red and exiting 0 sent
    # the shell on to the rm.
    return 1 if failed else 0


def cmd_stop_all(args: argparse.Namespace, out: Out) -> int:
    failed = 0
    for item in lifecycle.list_vms():
        if item.powered:
            if not args.quiet:
                out.step(f"stopping {item.name}")
            try:
                lifecycle.stop(
                    item.name,
                    mode=args.mode or ("force" if args.force else None),
                    preserve_run_state=args.preserve_run_state,
                )
            except VmhubError as exc:
                failed += 1
                if not args.quiet:
                    out.say(out.warn(f"  {exc}"))
    return 1 if failed else 0


def cmd_restore(args: argparse.Namespace, out: Out) -> int:
    # A restore that runs seconds into the boot races /run/user/<uid> (created by
    # the first login) and the access ACL on /dev/kvm (applied by udev a moment
    # after it creates the node). Losing either of those races looks exactly like
    # "the VM did not come back", so wait for them here rather than in whichever
    # init script happens to be installed.
    for problem in lifecycle.wait_until_podman_can_run():
        out.say(out.warn(f"  {problem}"))
    # A restore that brought nothing back is not a success: the boot hook reads
    # this exit status to decide whether to try again, and "nothing to restore"
    # is indistinguishable from "every VM failed" unless we say so here.
    failures: list[str] = []
    restored = lifecycle.restore_run_state(progress=out.step, failures=failures)
    if not restored and not failures and not args.quiet:
        out.step("nothing to restore")
    return 1 if failures else 0


def cmd_rm(args: argparse.Namespace, out: Out) -> int:
    names = args.names
    if not confirm(
        f"Delete {', '.join(names)} and their disks? This cannot be undone.",
        assume_yes=args.yes,
    ):
        out.say("aborted")
        return 1
    failed = 0
    for name in names:
        try:
            lifecycle.remove(name, keep_disk=args.keep_disk, progress=out.step)
        except VmhubError as exc:
            failed += 1
            out.say(out.bad(f"{name}: {exc}"))
    return 1 if failed else 0


def cmd_snapshot(args: argparse.Namespace, out: Out) -> int:
    lifecycle.take_snapshot(args.name, args.snapshot, progress=out.step)
    return 0


def cmd_snapshots(args: argparse.Namespace, out: Out) -> int:
    entries = lifecycle.list_snapshots(args.name)
    if not entries:
        out.say(out.dim("no snapshots"))
        return 0
    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["date-sec"]))
        out.say(f"  {entry['name']:24s} {when}")
    return 0


def cmd_revert(args: argparse.Namespace, out: Out) -> int:
    if not confirm(
        f"Revert {args.name} to snapshot '{args.snapshot}'? "
        "This discards all snapshots taken after it and all disk changes since.",
        assume_yes=args.yes,
    ):
        out.say("aborted")
        return 1
    lifecycle.revert(
        args.name,
        args.snapshot,
        progress=out.step,
        # None follows the snapshot's recorded power state; --no-restart
        # is the explicit override.
        restart=False if args.no_restart else None,
    )
    return 0


def cmd_snapshot_rm(args: argparse.Namespace, out: Out) -> int:
    lifecycle.delete_snapshot(args.name, args.snapshot, progress=out.step)
    return 0


def cmd_template(args: argparse.Namespace, out: Out) -> int:
    if args.action == "ls":
        found = registry.templates()
        if not found:
            out.say(out.dim("no templates"))
            return 0
        for meta in found:
            kids = registry.dependents_of(meta.name)
            suffix = out.dim(f"  <- {len(kids)} clone(s)") if kids else ""
            out.say(f"  {meta.name}{suffix}")
        return 0
    if args.action == "mark":
        lifecycle.mark_template(args.name, progress=out.step)
        return 0
    if args.action == "unmark":
        lifecycle.unmark_template(args.name, progress=out.step)
        return 0
    return 2


def _is_template(name: str) -> bool:
    try:
        return registry.load_meta(name).is_template
    except VmhubError:
        return False


def cmd_clone(args: argparse.Namespace, out: Out) -> int:
    lifecycle.clone(
        args.name,
        args.source,
        mode=args.mode or ("linked" if _is_template(args.source) else "full"),
        cpus=args.cpus,
        ram=args.ram,
        progress=out.step,
    )
    if not args.no_start:
        lifecycle.start(args.name, progress=out.step)
        url = lifecycle.console_url(args.name)
        if url:
            out.say()
            out.say(f"Console: {out.info(url)}")
    return 0


def cmd_flatten(args: argparse.Namespace, out: Out) -> int:
    lifecycle.flatten(args.name, progress=out.step)
    return 0


def cmd_rebase(args: argparse.Namespace, out: Out) -> int:
    updated = lifecycle.rebase_all(args.template, progress=out.step)
    out.say(out.good(f"rebased {len(updated)} clone(s) onto {args.template}"))
    return 0


def cmd_guest(args: argparse.Namespace, out: Out) -> int:
    """Ask the running guest for its IP/hostname/OS via the agent."""
    if not registry.exists(args.name):
        print(out.bad(f"no such VM: {args.name}"))
        return 1
    info = lifecycle.guest_info(args.name)
    if not info.reachable:
        out.say(out.warn(f"  {info.describe()}"))
        out.say(out.dim(
            "  Install qemu-guest-agent in the guest to use this, and 'stop --mode guest'."
        ))
        return 1
    if info.ip:
        out.say(f"  {'ip':10s} {info.ip}")
    if info.hostname:
        out.say(f"  {'hostname':10s} {info.hostname}")
    if info.os_name:
        out.say(f"  {'os':10s} {info.os_name}")
    return 0


def cmd_resize(args: argparse.Namespace, out: Out) -> int:
    lifecycle.resize(args.name, args.size, progress=out.step)
    return 0


def cmd_set(args: argparse.Namespace, out: Out) -> int:
    vm_spec = registry.load_spec(args.name)
    changed: list[str] = []
    for item in args.set or []:
        key, sep, value = item.partition("=")
        if not sep:
            print(out.bad(f"expected key=value, got {item!r}"))
            return 2
        target, _, leaf = key.rpartition(".")
        section = getattr(vm_spec, target, None) if target else vm_spec
        if section is None or not hasattr(section, leaf):
            print(out.bad(f"unknown spec key: {key}"))
            return 2
        current = getattr(section, leaf)
        try:
            coerced = _coerce(current, value, key)
        except ValueError as exc:
            print(out.bad(str(exc)))
            return 2
        setattr(section, leaf, coerced)
        changed.append(f"{key} = {coerced!r}" if coerced == "" else f"{key} = {coerced}")
    if not changed:
        print(out.bad("nothing to set"))
        return 2
    try:
        vm_spec.validate()
    except VmhubError as exc:
        print(out.bad(f"invalid change: {exc}"))
        return 2
    registry.save_spec(vm_spec)
    for line in changed:
        out.say(f"  {line}")
    out.say(out.dim("  (applies on next start)"))
    return 0


def _coerce(current: object, value: str, key: str) -> object:
    """Turn a CLI string into the field's own type, with a usable error.

    An empty value means "clear it" for strings — that is how `boot.iso=` works,
    which vmhub's own warnings tell people to run.
    """
    if isinstance(current, bool):
        return value.lower() in {"y", "yes", "true", "1", "on"}
    if isinstance(current, int):
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{key} expects a number, got {value!r}") from None
    if isinstance(current, list):
        try:
            return [int(p) for p in value.split(",") if p.strip()]
        except ValueError:
            raise ValueError(f"{key} expects a comma-separated list of numbers") from None
    return value


def cmd_show(args: argparse.Namespace, out: Out) -> int:
    vm_spec = registry.load_spec(args.name)
    print(paths.spec_path(args.name).read_text().rstrip())
    return 0


def cmd_logs(args: argparse.Namespace, out: Out) -> int:
    if args.follow:
        for line in podman.follow_logs(args.name):
            sys.stdout.write(line)
            sys.stdout.flush()
        return 0
    print(podman.logs(args.name, tail=args.lines))
    return 0


def cmd_console(args: argparse.Namespace, out: Out) -> int:
    url = lifecycle.console_url(args.name)
    if not url:
        print(out.bad(f"{args.name} has no published viewer port"))
        return 1
    out.say(out.info(url))
    return open_url(url)


def cmd_ssh(args: argparse.Namespace, out: Out) -> int:
    vm_spec = registry.load_spec(args.name)
    host_ssh = vm_spec.network.host_for("ssh")
    if not host_ssh:
        print(out.bad(f"{args.name} has no published SSH port"))
        return 1
    target = lifecycle.ssh_target(args.name)
    if args.print:
        out.say(f"ssh {target}")
        return 0
    if not shutil.which("ssh"):
        out.say(f"ssh {target}")
        return 0
    return subprocess.run(["ssh", "-p", str(host_ssh),
                           vm_spec.network.bind or "127.0.0.1"]).returncode


def cmd_rdp(args: argparse.Namespace, out: Out) -> int:
    tgt = rdp.target(args.name)
    if not tgt.available:
        print(out.bad(
            f"{args.name} has no published RDP port. Add one with: "
            f"vmctl ports add {args.name} --as rdp"
        ))
        return 1
    if args.check:
        ok, message = rdp.probe(args.name)
        out.say((out.good("OK  ") if ok else out.bad("FAIL")) + f"  {message}")
        return 0 if ok else 1
    if args.print:
        out.say(out.info(tgt.uri()))
        out.say(f"  {out.dim('remmina -c ' + tgt.uri())}   {out.dim('# prompts for the password')}")
        out.say(f"  {out.dim(' '.join(tgt.freerdp_argv()))}")
        if not tgt.user:
            out.say()
            out.say(out.warn("  no RDP user is set; set the account you made during setup:"))
            out.say(out.info(f"    vmctl set {args.name} media.rdp_user=YourName"))
        return 0
    try:
        out.say(out.good(rdp.launch(args.name)))
    except VmhubError as exc:
        print(out.bad(str(exc)))
        return 1
    return 0


def cmd_finish_install(args: argparse.Namespace, out: Out) -> int:
    actions = lifecycle.finish_install(args.name, progress=out.step)
    if actions:
        out.say(out.good(f"{args.name} will now boot from its disk"))
    return 0
