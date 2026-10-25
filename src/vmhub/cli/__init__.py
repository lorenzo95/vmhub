"""The vmctl command line, split into a package.

One file used to hold every command, the parser that wires them up and
the little that paints the output. It had grown past fifteen hundred
lines, and the parser alone was a third of that: reading a command
meant scrolling past every command that had nothing to do with it.

The split is by what a command is about:

    output.py    the Out class and the colours it paints with
    vms.py       the VMs themselves: list, create, start, stop, clone...
    media.py     install media, export, import, libvirt
    network.py   the published ports and how to move them
    env.py       the environment: doctor, config, blueprints, prune
    parser.py    the argument parser, which wires the commands up
    main.py      the entry point

Every name the rest of vmhub uses is re-exported here, so nothing
outside this package has to change: callers keep doing
`from . import cli` and then `cli.cmd_ports(...)`, and the tests keep
reaching for `cli.Out` and `cli.port_scope`. The one thing that did
change is the import path of the module itself: it is a package now,
so `python3 -m vmhub.cli` runs `cli/__main__.py`.
"""

from __future__ import annotations

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

from .main import main

from .media import (
    cmd_bundles,
    cmd_export,
    cmd_export_all,
    cmd_import,
    cmd_iso,
    cmd_libvirt,
)

from .network import cmd_ports, cmd_repair_ports, port_scope


from .output import Out, colour, confirm, open_url

from .parser import build_parser

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
    error_text,
)
