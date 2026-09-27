from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import autostart, blueprints, disk, export, iso as isomod, lifecycle, paths, podman, ports, rdp, registry, spec
from .errors import VmhubError

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def colour(enabled: bool) -> bool:
    return enabled and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class Out:
    def __init__(self) -> None:
        self.c = colour(True)

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.c else text

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)

    def bold(self, text: str) -> str:
        return self.paint(text, BOLD)

    def good(self, text: str) -> str:
        return self.paint(text, GREEN)

    def warn(self, text: str) -> str:
        return self.paint(text, YELLOW)

    def bad(self, text: str) -> str:
        return self.paint(text, RED)

    def info(self, text: str) -> str:
        return self.paint(text, CYAN)

    def say(self, message: str = "") -> None:
        print(message)

    def step(self, message: str) -> None:
        print(f"  {self.dim(message)}")


STATE_COLOURS = {"running": "good", "paused": "warn", "stopped": "dim", "exited": "bad"}


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def open_url(url: str) -> int:
    launcher = shutil.which("xdg-open") or shutil.which("gio")
    if not launcher:
        print(f"Open this URL in your browser: {url}")
        return 1
    args = [launcher, "open", url] if launcher.endswith("gio") else [launcher, url]
    return subprocess.run(args).returncode


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
    rows = [
        ("state", item.summary),
        ("blueprint", item.blueprint or "-"),
        ("image", meta.image or vm_spec.image.ref),
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
        ("host ports", ", ".join(f"{h}->{g}" for h, g in sorted(item.ports.items())) or "-"),
        ("guest ports", ", ".join(str(p) for p in item.guest_ports) or "-"),
        ("snapshots", ", ".join(item.snapshots) or "-"),
        ("boot media", item.boot_image or "-"),
        ("peer dial", ", ".join(item.peer_targets)
            if item.peer_targets
            else "loopback only (set network.bind=0.0.0.0 to expose)"),
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


def cmd_start(args: argparse.Namespace, out: Out) -> int:
    failed = 0
    for name in args.names:
        try:
            lifecycle.start(name, progress=None if args.quiet else out.step)
            if not args.quiet:
                out.say(out.good(f"{name} started"))
        except VmhubError as exc:
            failed += 1
            out.say(out.bad(f"{name}: {exc}"))
    if args.console and not failed:
        open_url(lifecycle.console_url(args.names[0]))
    return 1 if failed else 0


def cmd_stop(args: argparse.Namespace, out: Out) -> int:
    mode = args.mode or ("force" if args.force else None)
    names = args.names or [s.name for s in lifecycle.list_vms() if s.powered]
    if not names:
        out.say(out.dim("nothing running"))
        return 0
    for name in names:
        try:
            used = lifecycle.stop(
                name, mode=mode, progress=None if args.quiet else out.step
            )
            if not args.quiet:
                out.say(out.good(f"{name} stopped ({used})"))
        except VmhubError as exc:
            out.say(out.bad(f"{name}: {exc}"))
    return 0


def cmd_stop_all(args: argparse.Namespace, out: Out) -> int:
    for item in lifecycle.list_vms():
        if item.powered:
            if not args.quiet:
                out.step(f"stopping {item.name}")
            try:
                lifecycle.stop(item.name, mode=args.mode or ("force" if args.force else None))
            except VmhubError as exc:
                if not args.quiet:
                    out.say(out.warn(f"  {exc}"))
    return 0


def cmd_restore(args: argparse.Namespace, out: Out) -> int:
    restored = lifecycle.restore_run_state(progress=out.step)
    if not restored and not args.quiet:
        out.step("nothing to restore")
    return 0


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
    lifecycle.revert(args.name, args.snapshot, progress=out.step, restart=not args.no_restart)
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


def cmd_clone(args: argparse.Namespace, out: Out) -> int:
    lifecycle.clone(
        args.name,
        args.template,
        mode=args.mode or "linked",
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


def cmd_iso(args: argparse.Namespace, out: Out) -> int:
    target, slot, source = args.target, args.slot, args.file

    if args.action == "probe":
        if not target:
            print(out.bad("usage: vmctl iso probe <file.iso>"))
            return 2
        info = isomod.probe(Path(target))
        out.say(f"  {info.name}: {info.describe()}")
        return 0 if info.is_iso else 1

    if not target:
        print(out.bad(f"usage: vmctl iso {args.action} <vm> [slot] [file]"))
        return 2

    if args.action == "ls":
        if not registry.exists(target):
            print(out.bad(f"no such VM: {target}"))
            return 1
        for entry in lifecycle.iso_status(target):
            if not entry["attached"]:
                out.say(f"  {entry['label']:34s} {out.dim('(not attached)')}")
                continue
            info = entry["info"]
            detail = info.describe() if info else out.bad("FILE MISSING")
            out.say(f"  {entry['label']:34s} {detail}")
            out.say(f"  {'':34s} {out.dim(entry['path'])}")
        return 0

    if not slot:
        print(out.bad(f"which slot? one of: {', '.join(sorted(isomod.SLOTS))}"))
        return 2

    if args.action == "attach":
        if not source:
            print(out.bad(f"usage: vmctl iso attach <vm> {slot} <file.iso>"))
            return 2
        info = lifecycle.attach_iso(target, slot, Path(source), progress=out.step)
        out.say(out.good(f"attached {info.name} to {target} [{slot}]"))
        return 0

    if args.action == "detach":
        if lifecycle.detach_iso(target, slot, progress=out.step):
            out.say(out.good(f"detached {slot} ISO from {target}"))
        return 0
    return 2


def cmd_export(args: argparse.Namespace, out: Out) -> int:
    archive = export.export_vm(args.name, dest_dir=Path(args.dest) if args.dest else None, progress=out.step)
    out.say(out.good(str(archive)))
    return 0


def cmd_import(args: argparse.Namespace, out: Out) -> int:
    name = export.import_bundle(
        Path(args.bundle), name=args.name, keep_identity=args.keep_identity, progress=out.step
    )
    out.say(out.good(f"imported as {name}"))
    return 0


def cmd_libvirt(args: argparse.Namespace, out: Out) -> int:
    from . import libvirt as libvirt_mod

    try:
        if args.action == "ls":
            found = libvirt_mod.names()
            if not found:
                out.say(out.dim("no libvirt domains (or libvirt refused the connection)"))
                return 0
            for name in found:
                try:
                    d = libvirt_mod.describe(name)
                except VmhubError as exc:
                    out.say(f"  {name:24s} {out.bad(str(exc))}")
                    continue
                out.say(
                    f"  {name:24s} {d.state:10s} {d.ram:>5s} {d.vcpus:>2d} vcpu  "
                    f"{len(d.disks)} disk(s): {', '.join(Path(p).name for p in d.disks)}"
                )
            return 0
        target = libvirt_mod.import_domain(
            args.name,
            new_name=args.new_name,
            disk_type=args.disk_type,
            source_index=args.disk_index,
            move=args.move,
            progress=out.step,
        )
        out.say(out.good(f"imported {args.name} as {target}"))
        return 0
    except VmhubError as exc:
        print(out.bad(str(exc)))
        return 1


def cmd_export_all(args: argparse.Namespace, out: Out) -> int:
    """Export every VM as a portable bundle.

    Running VMs are skipped by default because exporting needs the disk quiesced;
    --include-running stops them, exports, then restarts. Intended to be called
    from a backup tool (luckybackup pre-command, cron) as much as by hand.
    """
    dest = Path(args.dest) if args.dest else None
    wanted = set(args.names or [])
    statuses = sorted(lifecycle.list_vms(), key=lambda s: s.name)
    if wanted:
        unknown = wanted - {s.name for s in statuses}
        if unknown:
            print(out.bad(f"no such VM: {', '.join(sorted(unknown))}"))
            return 1
        statuses = [s for s in statuses if s.name in wanted]
    if not statuses:
        out.say(out.dim("no VMs"))
        return 0

    exported: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for status in statuses:
        if status.powered and not args.include_running:
            skipped.append(status.name)
            continue
        was_running = status.powered
        try:
            if was_running:
                lifecycle.stop(status.name, mode=args.mode, progress=out.step)
            archive = export.export_vm(status.name, dest_dir=dest, progress=out.step)
            exported.append(status.name)
            out.say(out.good(f"  {status.name} -> {archive.name}"))
        except VmhubError as exc:
            failed.append((status.name, str(exc)))
            out.say(out.bad(f"  {status.name}: {exc}"))
        finally:
            if was_running:
                try:
                    lifecycle.start(status.name, progress=out.step)
                except VmhubError as exc:
                    failed.append((status.name, f"restart failed: {exc}"))
                    out.say(out.bad(f"  {status.name}: could not restart: {exc}"))

    out.say()
    out.say(
        f"  exported {len(exported)}, skipped {len(skipped)}"
        + (f" (running: {', '.join(skipped)})" if skipped else "")
        + (f", failed {len(failed)}" if failed else "")
    )
    if skipped:
        out.say(out.dim("  pass --include-running to stop and restart them around the export"))
    return 1 if failed else 0


def cmd_bundles(args: argparse.Namespace, out: Out) -> int:
    entries = export.list_bundles(Path(args.dest) if args.dest else None)
    if not entries:
        out.say(out.dim("no bundles"))
        return 0
    for entry in entries:
        if "error" in entry:
            out.say(f"  {out.bad('!')} {Path(entry['path']).name}: {entry['error']}")
            continue
        out.say(
            f"  {entry['name']:20s} {entry.get('created_iso',''):26s} "
            f"{spec.format_size(entry.get('virtual_size',0)):>7s} virtual  "
            f"{out.dim(str(Path(entry['path']).name))}"
        )
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
    if not vm_spec.network.host_ssh:
        print(out.bad(f"{args.name} has no published SSH port"))
        return 1
    target = lifecycle.ssh_target(args.name)
    if args.print:
        out.say(f"ssh {target}")
        return 0
    if not shutil.which("ssh"):
        out.say(f"ssh {target}")
        return 0
    return subprocess.run(["ssh", "-p", str(vm_spec.network.host_ssh),
                           vm_spec.network.bind or "127.0.0.1"]).returncode


def cmd_rdp(args: argparse.Namespace, out: Out) -> int:
    tgt = rdp.target(args.name)
    if not tgt.available:
        print(out.bad(
            f"{args.name} has no published RDP port. Add one with: "
            f"vmctl set {args.name} network.guest_ports=3389"
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


def cmd_blueprints(args: argparse.Namespace, out: Out) -> int:
    for item in blueprints.iter_all():
        out.say(out.bold(item.title or item.name))
        out.say(f"  {out.dim(item.name)}  {item.description}")
        try:
            base = item.base_spec("preview")
            out.say(
                out.dim(
                    f"  default: {base.resources.cpus} cpu, {base.resources.ram} ram, "
                    f"{base.resources.disk} disk, {base.resources.disk_type}, "
                    f"guest ports {base.network.guest_ports}"
                )
            )
        except VmhubError as exc:
            out.say(out.bad(f"  invalid blueprint: {exc}"))
        out.say()
    return 0


def cmd_prune(args: argparse.Namespace, out: Out) -> int:
    orphans = podman.orphans()
    if not orphans:
        out.say(out.good("no orphaned containers"))
        return 0
    for entry in orphans:
        out.say(
            f"  {entry['container']:34s} {entry['state']:8s} "
            f"its VM directory {entry['vm']!r} is gone"
        )
    if args.dry_run:
        out.say()
        out.say(out.dim("  dry run; nothing removed"))
        return 0
    if not args.yes and not confirm("Remove these containers?", assume_yes=False):
        out.say("aborted")
        return 1
    removed = podman.prune_orphans(keep_running=args.keep_running)
    for line in removed:
        out.say(out.good(f"removed {line}"))
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


def cmd_doctor(args: argparse.Namespace, out: Out) -> int:
    problems = 0
    out.say(out.bold("vmhub doctor"))
    runtime = podman.oci_runtime() if shutil.which("podman") else "NOT FOUND"
    if runtime == "crun":
        runtime_note = "crun"
    elif runtime in {"", "NOT FOUND"}:
        runtime_note = runtime
    else:
        runtime_note = f"{runtime} (crun recommended for rootless)"
    helpers = [h for h in ("newuidmap", "newgidmap") if not shutil.which(h)]
    net_helper = next(
        (h for h in ("pasta", "passt", "slirp4netns") if shutil.which(h)), ""
    )
    gui_missing = [
        m for m in ("gi", "gi.repository.Gtk")
        if importlib.util.find_spec(m) is None
    ]
    checks = [
        ("project root", str(paths.project_root())),
        ("podman", podman.version() if shutil.which("podman") else "NOT FOUND"),
        ("podman rootless", "yes" if podman.is_rootless() else "NO - containers run as root"),
        ("oci runtime", runtime_note),
        ("uidmap", "installed" if not helpers else f"MISSING: {', '.join(helpers)}"),
        ("rootless net", net_helper or "MISSING: passt (or slirp4netns)"),
        ("qemu-img", disk.binary_version()),
        ("host /dev/kvm", "readable" if podman.host_has_kvm() else "NOT ACCESSIBLE"),
        ("python gi/gtk", "importable" if not gui_missing else f"MISSING: {', '.join(gui_missing)}"),
        ("free space", spec.format_size(os.statvfs(paths.project_root()).f_bavail * os.statvfs(paths.project_root()).f_frsize)),
        ("init system", autostart.detect_init()),
        ("vms", str(len(registry.all_names()))),
    ]
    for label, value in checks:
        bad = "NOT" in str(value) or str(value) == "no" or "MISSING" in str(value)
        problems += 1 if bad else 0
        out.say(f"  {label:16s} {out.bad(value) if bad else value}")
    if runtime not in {"crun", "NOT FOUND", ""}:
        out.say(out.dim(
            "                   --group-add keep-groups needs crun: apt-get install crun"
        ))
    for entry in podman.orphans():
        problems += 1
        out.say(
            f"  {out.warn('orphan')}        container {entry['container']} is "
            f"{entry['state']} but VM {entry['vm']!r} no longer exists — "
            f"it will never be stopped again. Clean up: vmctl prune"
        )

    for name, reason in registry.broken_metas():
        problems += 1
        out.say(
            f"  {out.bad('bad meta')}     {name}/meta.json cannot be read ({reason}); "
            f"the VM cannot be started or snapshotted until it is fixed"
        )

    for port, names in sorted(ports.collisions().items()):
        out.say(
            f"  {out.warn('port clash')}      host port {port} is claimed by "
            f"{', '.join(names)} — only one of them can start. Fix: vmctl repair-ports"
        )
        problems += 1


    for vm in registry.all_names():
        try:
            vm_spec = registry.load_spec(vm)
        except VmhubError as exc:
            problems += 1
            out.say(f"  {out.bad('spec error')}  {vm}: {exc}")
            continue
        for warning in ports.verify(vm_spec, check_busy=False):
            out.say(f"  {out.warn('warning')}       {vm}: {warning}")
    out.say()
    out.say(out.bad(f"{problems} problem(s)") if problems else out.good("all good"))
    return 1 if problems else 0


def cmd_autostart(args: argparse.Namespace, out: Out) -> int:
    if args.action == "status":
        for key, value in autostart.status().items():
            out.say(f"  {key:20s} {value}")
        return 0
    if args.action == "install":
        for line in autostart.install(target=args.target, gui=args.gui):
            out.say(f"  {out.good('+')} {line}")
        return 0
    if args.action == "uninstall":
        for line in autostart.uninstall(target=args.target):
            out.say(f"  {out.good('-')} {line}")
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmctl", description="Manage QEMU VMs running in rootless podman containers."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ls", help="list VMs")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("info", help="show details for one VM")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("new", help="create a VM")
    p.add_argument("name")
    p.add_argument("--blueprint", "-b")
    p.add_argument("--no-blueprint", action="store_true")
    p.add_argument("--cpus", type=int)
    p.add_argument("--ram")
    p.add_argument("--disk", dest="disk")
    p.add_argument("--disk-type")
    p.add_argument("--boot-mode", choices=sorted(spec.BOOT_MODES))
    p.add_argument("--iso", help="boot image keyword or URL")
    p.add_argument("--image", help="container image, e.g. docker.io/qemux/qemu:latest")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--mark-template", action="store_true")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("start", help="start VMs")
    p.add_argument("names", nargs="+", help="one or more VM names")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress per-VM output")
    p.add_argument("--console", action="store_true", help="open the web console")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop VMs (default: graceful ACPI shutdown)")
    p.add_argument("names", nargs="*", help="VM names; all running VMs if omitted")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress per-VM output")
    p.add_argument(
        "--mode",
        choices=list(lifecycle.STOP_MODES),
        help=(
            "guest: ask the in-guest agent to shut down (cleanest, needs the agent); "
            "graceful: ACPI, waits shutdown.timeout seconds; "
            "power: cut power via QMP, container still shuts down cleanly; "
            "force: SIGKILL, no guest shutdown and no cleanup"
        ),
    )
    p.add_argument("--force", action="store_true", help="alias for --mode force")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("stop-all", help="stop every running VM")
    p.add_argument("--mode", choices=list(lifecycle.STOP_MODES))
    p.add_argument("--force", action="store_true", help="alias for --mode force")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_stop_all)

    p = sub.add_parser("restore", help="restart VMs that were running at last shutdown")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("rm", help="delete VMs and their disks")
    p.add_argument("names", nargs="+")
    p.add_argument(
        "--keep-disk", action="store_true",
        help="remove the container but keep the VM and its disk (still startable)",
    )
    p.add_argument("--yes", "-y", action="store_true", help="do not prompt")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("snapshot", help="take an instantaneous copy-on-write snapshot")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("snapshots", help="list snapshots")
    p.add_argument("name")
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("revert", help="revert to a snapshot (discards later snapshots)")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--no-restart", action="store_true")
    p.set_defaults(func=cmd_revert)

    p = sub.add_parser("snapshot-rm", help="delete a snapshot")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.set_defaults(func=cmd_snapshot_rm)

    p = sub.add_parser("template", help="manage templates")
    p.add_argument("action", choices=["ls", "mark", "unmark"])
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("clone", help="clone from a template")
    p.add_argument("name")
    p.add_argument("template")
    p.add_argument(
        "--mode",
        choices=list(lifecycle.CLONE_MODES),
        help=(
            "linked: instant copy-on-write overlay sharing the template's disk; "
            "full: standalone copy, independent but copies real data"
        ),
    )
    p.add_argument("--cpus", type=int)
    p.add_argument("--ram")
    p.add_argument("--no-start", action="store_true")
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("flatten", help="resolve backing chain into a standalone disk")
    p.add_argument("name")
    p.set_defaults(func=cmd_flatten)

    p = sub.add_parser("rebase", help="repoint all clones of a template at a new disk")
    p.add_argument("template")
    p.set_defaults(func=cmd_rebase)

    p = sub.add_parser("libvirt", help="list or import libvirt domains")
    p.add_argument("action", choices=["ls", "import"])
    p.add_argument("name", nargs="?", help="domain to import")
    p.add_argument("--new-name", help="name for the vmhub VM (default: the domain's)")
    p.add_argument("--disk-type", default="ide", choices=sorted(spec.DISK_TYPES),
                   help="disk bus for the imported VM (default ide, widest compatibility)")
    p.add_argument("--disk-index", type=int, default=0, help="which disk to import")
    p.add_argument("--move", action="store_true",
                   help="delete the original libvirt disk after a successful convert")
    p.set_defaults(func=cmd_libvirt)

    p = sub.add_parser("guest", help="query the guest agent for ip/hostname/os")
    p.add_argument("name")
    p.set_defaults(func=cmd_guest)

    p = sub.add_parser("iso", help="attach or detach installation media")
    p.add_argument("action", choices=["ls", "attach", "detach", "probe"])
    p.add_argument("target", nargs="?", help="VM name, or an ISO path for 'probe'")
    p.add_argument("slot", nargs="?", choices=sorted(isomod.SLOTS))
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_iso)

    p = sub.add_parser("export", help="export a self-contained portable bundle")
    p.add_argument("name")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import a bundle")
    p.add_argument("bundle")
    p.add_argument("--name")
    p.add_argument("--keep-identity", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export-all", help="export every VM (or the named ones)")
    p.add_argument("names", nargs="*", help="limit to these VMs")
    p.add_argument("--dest", help="destination directory (default: backups/)")
    p.add_argument("--include-running", action="store_true",
                   help="stop running VMs, export, then restart them")
    p.add_argument("--mode", choices=list(lifecycle.STOP_MODES), default="graceful",
                   help="how to stop running VMs when --include-running is used")
    p.set_defaults(func=cmd_export_all)

    p = sub.add_parser("bundles", help="list exported bundles")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_bundles)

    p = sub.add_parser("backup", help="alias for export")
    p.add_argument("name")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("resize", help="grow a disk (never shrinks)")
    p.add_argument("name")
    p.add_argument("size")
    p.set_defaults(func=cmd_resize)

    p = sub.add_parser("set", help="edit spec values, e.g. set vm resources.cpus=8")
    p.add_argument("name")
    p.add_argument("set", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("show", help="print vm.toml")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("logs", help="container logs")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("console", help="open the web console")
    p.add_argument("name")
    p.set_defaults(func=cmd_console)

    p = sub.add_parser("ssh", help="ssh into a VM")
    p.add_argument("name")
    p.add_argument("--print", action="store_true")
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("rdp", help="connect to a Windows VM over RDP via Remmina or FreeRDP")
    p.add_argument("name")
    p.add_argument("--print", action="store_true", help="show the URI and FreeRDP command")
    p.add_argument("--check", action="store_true", help="probe the RDP path and report failures")
    p.set_defaults(func=cmd_rdp)

    p = sub.add_parser("finish-install", help="clear the boot ISO after installing a guest")
    p.add_argument("name")
    p.set_defaults(func=cmd_finish_install)

    p = sub.add_parser("blueprints", help="list available blueprints")
    p.set_defaults(func=cmd_blueprints)

    p = sub.add_parser("doctor", help="check the environment")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("prune", help="remove containers whose VM directory is gone")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument(
        "--keep-running", action="store_true",
        help="skip orphans that are still running (e.g. holding 16 GB of RAM)",
    )
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("repair-ports", help="reassign conflicting host ports")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_repair_ports)

    p = sub.add_parser("autostart", help="install boot/login restore hooks")
    p.add_argument("action", choices=["status", "install", "uninstall"])
    p.add_argument("--target", choices=["systemd", "sysvinit", "session"])
    p.add_argument("--gui", action="store_true", help="also start the GUI with the session")
    p.set_defaults(func=cmd_autostart)


    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out()
    try:
        return args.func(args, out) or 0
    except KeyboardInterrupt:
        print()
        return 130
    except VmhubError as exc:
        print(out.bad(f"error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
