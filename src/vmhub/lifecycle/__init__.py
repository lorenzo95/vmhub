"""The lifecycle API, assembled from the pieces it is made of.

Every name the rest of vmhub uses is re-exported here, so nothing
outside this package has to change: callers keep doing
`from . import lifecycle` and then `lifecycle.start(...)`, and
export.py keeps doing `from .lifecycle import Progress, _noop`.
"""

from __future__ import annotations

from .clone import (
    CLONE_MODES,
    clone,
    flatten,
    mark_template,
    rebase_all,
    unmark_template,
)

from .lifecycle import (
    QEMU_READY_STATES,
    QEMU_READY_TIMEOUT,
    STOP_MODES,
    create,
    finish_install,
    remove,
    resize,
    restore_run_state,
    start,
    stop,
    stop_all,
)

from .media import attach_iso, detach_iso, iso_status

from .progress import Progress, _noop

from .snapshot import delete_snapshot, list_snapshots, revert, take_snapshot

from .status import (
    CONTAINER_MEDIA,
    Ctx,
    Notice,
    VmStatus,
    boot_image,
    console_url,
    default_host_address,
    disk_has_data,
    drifted,
    guest_info,
    host_addresses,
    list_vms,
    live_media,
    load_ctx,
    peer_address,
    peer_dial_targets,
    spec_user_hint,
    ssh_target,
    status,
    template_disk,
    viewer_port,
)

__all__ = [
    "Notice",
    "VmStatus",
    "Ctx",
    "Progress",
    "_noop",
    "STOP_MODES",
    "CLONE_MODES",
    "QEMU_READY_TIMEOUT",
    "QEMU_READY_STATES",
    "CONTAINER_MEDIA",
    "load_ctx",
    "template_disk",
    "create",
    "clone",
    "flatten",
    "rebase_all",
    "mark_template",
    "unmark_template",
    "finish_install",
    "start",
    "stop",
    "stop_all",
    "remove",
    "resize",
    "restore_run_state",
    "status",
    "list_vms",
    "console_url",
    "live_media",
    "drifted",
    "boot_image",
    "disk_has_data",
    "host_addresses",
    "default_host_address",
    "peer_address",
    "peer_dial_targets",
    "guest_info",
    "viewer_port",
    "ssh_target",
    "spec_user_hint",
    "list_snapshots",
    "take_snapshot",
    "delete_snapshot",
    "revert",
    "attach_iso",
    "detach_iso",
    "iso_status",
]
