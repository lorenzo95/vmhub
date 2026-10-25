"""The commands that are about media, bundles and other machines."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import disk, export, iso as isomod, lifecycle, registry, spec
from ..errors import VmhubError
from .output import Out


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
