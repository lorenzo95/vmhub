"""The commands that are about the machine vmhub runs on."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil

from .. import autostart, blueprints, desktop, disk, lifecycle, paths, podman, ports, registry, settings, spec
from ..errors import ProjectRootError, VmhubError
from .output import Out, confirm


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


def cmd_config(args: argparse.Namespace, out: Out) -> int:
    """Print the app-wide defaults, and where they live."""
    values = settings.load()
    out.say(out.bold("KEY"))
    for key, description in sorted(settings.FIELDS.items()):
        value = values.get(key, "").strip()
        if value:
            out.say(f"{key:<14} {value}")
        else:
            # Show what the field resolves to, not "(unset)". Asked what a new
            # VM will pull, "(unset)" says nothing and the answer -- the
            # shipped pin -- appears nowhere on the screen. The wording keeps
            # the difference visible: a dimmed value is one you did not choose.
            fallback = settings.fallback(key)
            if fallback:
                out.say(f"{key:<14} {out.dim(f'{fallback}  (shipped default)')}")
            else:
                out.say(f"{key:<14} {out.dim('(unset)')}")
        out.say(out.dim(f"               {description}"))
    out.say()
    out.say(out.dim(f"stored in {settings.config_path()}"))
    return 0


def cmd_config_set(args: argparse.Namespace, out: Out) -> int:
    if args.key not in settings.FIELDS:
        out.say(out.bad(f"unknown setting {args.key!r}"))
        out.say(out.dim(f"known settings: {', '.join(sorted(settings.FIELDS))}"))
        return 2
    try:
        path = settings.set_value(args.key, args.value)
    except settings.SettingError as exc:
        # A traceback is the worst possible answer to a typo in a tag. Say
        # which value and why, the way every other complaint here is said.
        out.say(out.bad(str(exc)))
        return 1
    except OSError as exc:
        out.say(out.bad(f"could not write {settings.config_path()}: {exc}"))
        return 1
    out.say(f"{args.key} = {args.value or out.dim('(unset)')}")
    out.say(out.dim(f"written to {path}"))
    return 0


def cmd_config_path(args: argparse.Namespace, out: Out) -> int:
    out.say(str(settings.config_path()))
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
    # doctor exists to report a broken environment, so it must survive the one
    # condition it is here to diagnose: resolve the root once, then degrade.
    root_problem = ""
    try:
        root = paths.project_root()
    except ProjectRootError as exc:
        root = None
        root_problem = str(exc)
    # Each row carries its own verdict. Deriving it from the rendered text,
    # as "NOT" in value or value == "no" did, passed a root podman: the
    # row reads "NO - containers run as root", which matches none of those
    # substrings, so the worst outcome for a rootless tool read as healthy.
    # It also flagged any innocent value containing those words.
    try:
        qemu_img, qemu_img_ok = disk.binary_version(), True
    except VmhubError as exc:
        # Used to propagate out of cmd_doctor entirely, so a missing
        # qemu-img aborted the table instead of being reported in it.
        qemu_img, qemu_img_ok = str(exc), False
    has_podman = shutil.which("podman") is not None
    # Report the image new VMs will pull and whether it is here. It follows
    # the image_tag preference, so this is the user's choice and not always
    # the shipped pin -- the label stays "qemu image" rather than claiming to
    # name a pin. Deliberately verdict-free: an unpulled image is pulled on
    # first start, so failing doctor over it would be wrong.
    default_image = spec.Image().ref
    if not has_podman:
        default_image_state = "unknown"
    elif podman.image_present(default_image):
        default_image_state = "local"
    else:
        default_image_state = "not pulled"
    rootless = podman.is_rootless()
    has_kvm = podman.host_has_kvm()
    checks = [
        ("project root", "NOT FOUND: " + root_problem if root_problem else str(root), root is not None),
        ("podman", podman.version() if has_podman else "NOT FOUND", has_podman),
        ("podman rootless", "yes" if rootless else "NO - containers run as root", rootless),
        ("oci runtime", runtime_note, runtime != "NOT FOUND"),
        ("uidmap", "installed" if not helpers else f"MISSING: {', '.join(helpers)}", not helpers),
        ("rootless net", net_helper or "MISSING: passt (or slirp4netns)", bool(net_helper)),
        ("qemu-img", qemu_img, qemu_img_ok),
        ("host /dev/kvm", "readable" if has_kvm else "NOT ACCESSIBLE", has_kvm),
        ("qemu image", f"{default_image}  ({default_image_state})", True),
        ("python gi/gtk", "importable" if not gui_missing else f"MISSING: {', '.join(gui_missing)}", not gui_missing),
        ("free space", spec.format_size(os.statvfs(root).f_bavail * os.statvfs(root).f_frsize) if root else "", True),
        ("init system", autostart.detect_init(), True),
        ("vms", str(len(registry.all_names())) if root else "", True),
    ]
    for label, value, ok in checks:
        problems += 0 if ok else 1
        out.say(f"  {label:16s} {value if ok else out.bad(value)}")
    if runtime not in {"crun", "NOT FOUND", ""}:
        out.say(out.dim(
            "                   --group-add keep-groups needs crun: apt-get install crun"
        ))
    if root_problem:
        # Every check below enumerates VMs through the project root, so running
        # them would only repeat this same failure. Report and exit non-zero.
        out.say()
        out.say(out.bad(f"{problems} problem(s)"))
        return 1
    for entry in podman.orphans():
        problems += 1
        out.say(
            f"  {out.warn('orphan')}        container {entry['container']} is "
            f"{entry['state']} but VM {entry['vm']!r} no longer exists — "
            f"it will never be stopped again. Clean up: vmctl prune"
        )

    for item in lifecycle.list_vms():
        if not item.leftover:
            continue
        problems += 1
        held = (
            f" and holding host port {', '.join(str(p) for p in sorted(item.leftover_ports))}"
            if item.leftover_ports
            else ""
        )
        out.say(
            f"  {out.warn('leftover')}      {item.name}: podman lost this VM's container, "
            f"but {', '.join(str(pid) for pid in item.leftover_pids)} is still running"
            f"{held} — the VM is up and vmhub cannot manage it. "
            f"Clear it: vmctl stop {item.name}"
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


def cmd_desktop(args: argparse.Namespace, out: Out) -> int:
    if args.action == "status":
        for key, value in desktop.status().items():
            out.say(f"  {key:16s} {value}")
        return 0
    if args.action == "install":
        for line in desktop.install():
            out.say(f"  {out.good('+')} {line}")
        return 0
    for line in desktop.uninstall():
        out.say(f"  {out.good('-')} {line}")
    return 0
