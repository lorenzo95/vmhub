"""The argument parser, and the wiring of every command into it."""

from __future__ import annotations

import argparse
import json
import os

from .env import (
    cmd_autostart,
    cmd_blueprints,
    cmd_config,
    cmd_config_path,
    cmd_config_set,
    cmd_desktop,
    cmd_doctor,
    cmd_prune,
)
from .media import (
    cmd_bundles,
    cmd_export,
    cmd_export_all,
    cmd_import,
    cmd_iso,
    cmd_libvirt,
)
from .network import cmd_ports, cmd_repair_ports
from .vms import (
    cmd_clone,
    cmd_console,
    cmd_finish_install,
    cmd_flatten,
    cmd_guest,
    cmd_info,
    cmd_logs,
    cmd_ls,
    cmd_new,
    cmd_rdp,
    cmd_rebase,
    cmd_resize,
    cmd_restore,
    cmd_revert,
    cmd_rm,
    cmd_set,
    cmd_show,
    cmd_snapshot,
    cmd_snapshot_rm,
    cmd_snapshots,
    cmd_ssh,
    cmd_start,
    cmd_stop,
    cmd_stop_all,
    cmd_template,
)

from .. import autostart, blueprints, desktop, disk, export, iso as isomod, lifecycle, podman, ports, rdp, settings, spec


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
    p.add_argument("--image", help=f"container image, e.g. {spec.Image().ref}")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--mark-template", action="store_true")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("start", help="start VMs")
    p.add_argument("names", nargs="+", help="one or more VM names")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress per-VM output")
    p.add_argument("--console", action="store_true", help="open the web console")
    p.add_argument(
        "--repair-ports",
        action="store_true",
        help=(
            "reassign host ports that are taken instead of refusing to start. "
            "Without this, start() will not touch the ports in vm.toml."
        ),
    )
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
    p.add_argument(
        "--preserve-run-state", action="store_true",
        help="stop the VMs but leave meta.last_run_state as it was, so a "
             "shutdown stop does not cancel `vmctl restore` on the next boot",
    )
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

    p = sub.add_parser("clone", help="clone an existing VM")
    p.add_argument("name", help="name for the new clone")
    p.add_argument("source", help="VM to clone (a template for --mode linked)")
    p.add_argument(
        "--mode",
        choices=list(lifecycle.CLONE_MODES),
        help=(
            "linked: instant copy-on-write overlay on the source disk, which must "
            "be a template; full: standalone copy, independent but copies real "
            "data. Defaults to linked for a template, full otherwise"
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

    p = sub.add_parser(
        "config",
        help="show or change the app-wide defaults (RDP account, shared folder)",
    )
    config_sub = p.add_subparsers(dest="config_action")

    sp = config_sub.add_parser("show", help="print the current defaults (default)")
    sp.set_defaults(func=cmd_config)

    sp = config_sub.add_parser("set", help="change one default")
    sp.add_argument("key", help=f"one of: {', '.join(sorted(settings.FIELDS))}")
    sp.add_argument("value", help="the new value; pass an empty string to unset")
    sp.set_defaults(func=cmd_config_set)

    sp = config_sub.add_parser("path", help="print where the defaults are stored")
    sp.set_defaults(func=cmd_config_path)

    # `vmctl config` on its own shows them, the way `vmctl ports <name>` lists.
    p.set_defaults(func=cmd_config, config_action="show")

    p = sub.add_parser("ports", help="show or change a VM's published ports")
    p.add_argument("name", help="the VM")
    ports_sub = p.add_subparsers(dest="ports_action")

    sp = ports_sub.add_parser("list", help="list published ports (default)")
    sp.set_defaults(func=cmd_ports)

    sp = ports_sub.add_parser("add", help="publish a guest port")
    sp.add_argument("guest", nargs="?", type=int, help="guest port, e.g. 5432")
    sp.add_argument("--host", type=int, help="host port (default: the first free one)")
    sp.add_argument("--name", dest="label", default="", help="label shown for the entry")
    sp.add_argument(
        "--bind", default="",
        help="per-port bind address (127.0.0.1, 0.0.0.0, or *). Default: use network.bind. 0.0.0.0 exposes the port outside this host.",
    )
    sp.add_argument(
        "--as", dest="role", choices=list(spec.PORT_ROLES),
        help="treat this as vmhub's web, ssh or rdp port",
    )
    sp.set_defaults(func=cmd_ports)

    sp = ports_sub.add_parser("rm", help="stop publishing a port")
    sp.add_argument("target", help="role (web, ssh, rdp) or the host port number")
    sp.set_defaults(func=cmd_ports)

    sp = ports_sub.add_parser("set", help="change a port's host or guest number")
    sp.add_argument("target", help="role (web, ssh, rdp) or the host port number")
    sp.add_argument("--host", type=int, help="the new host port")
    sp.add_argument(
        "--guest", type=int,
        help="the new guest port -- use this to correct a mistyped guest number",
    )
    sp.add_argument(
        "--bind", default=None,
        help="the new per-port bind address (use empty string to inherit network.bind)",
    )
    sp.set_defaults(func=cmd_ports)

    p.set_defaults(func=cmd_ports, ports_action="list")

    p = sub.add_parser("repair-ports", help="reassign conflicting host ports")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_repair_ports)

    p = sub.add_parser("desktop", help="install or remove the start-menu entry")
    p.add_argument("action", choices=["status", "install", "uninstall"])
    p.set_defaults(func=cmd_desktop)

    p = sub.add_parser("autostart", help="install boot/login restore hooks")
    p.add_argument("action", choices=["status", "install", "uninstall"])
    p.add_argument("--target", choices=["systemd", "sysvinit", "session"])
    p.add_argument("--gui", action="store_true", help="also start the GUI with the session")
    p.set_defaults(func=cmd_autostart)


    return parser
