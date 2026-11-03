"""Waiting for cloud-init to finish, over the guest agent.

Provisioning is not done when the container is up. It is done when the
guest has applied its seed, which is the last thing cloud-init does. The
guest agent is a virtio-serial device, so this check works even when the
network (and therefore SSH) is not up yet -- which is exactly the state a
broken network-config leaves you in.

`guest-exec` is used rather than reading a log because cloud-init writes
/var/lib/cloud/instance/boot-finished itself, and that file is the
publisher's own definition of "done" rather than a string we guessed.
"""

from __future__ import annotations

import time
from typing import Callable

from . import paths, qga
from .errors import CloudError, NotFound, QgaError

Progress = Callable[[str], None]

BOOT_FINISHED = "/var/lib/cloud/instance/boot-finished"
DEFAULT_TIMEOUT = 420.0
POLL = 2.0


def _guest_run(client: qga.Qga, argv: list[str], *, timeout: float = 15.0) -> dict:
    result = client.execute(
        "guest-exec",
        path=argv[0],
        arg=argv[1:],
        **{"capture-output": True},
    )
    pid = int(result.get("pid") or 0)
    if not pid:
        raise QgaError(f"guest-exec returned no pid: {result}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.execute("guest-exec-status", pid=pid) or {}
        if status.get("exited"):
            return status
        time.sleep(0.1)
    raise QgaError(f"guest command did not finish within {timeout:g}s: {' '.join(argv)}")


def boot_finished(vm: str, *, timeout: float = 6.0) -> bool:
    """One check, for callers that do not want to wait."""
    path = paths.storage_dir(vm) / paths.QGA_SOCKET
    if not path.exists():
        return False
    try:
        with qga.Qga(path, timeout=timeout) as client:
            status = _guest_run(
                client, ["/bin/sh", "-c", f"test -f {BOOT_FINISHED}"], timeout=timeout
            )
    except (QgaError, NotFound, OSError):
        return False
    return int(status.get("exitcode") or 0) == 0


def wait(
    vm: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Progress | None = None,
) -> bool:
    """Block until cloud-init has applied the seed, or explain what stopped it."""
    say = progress or (lambda _m: None)
    path = paths.storage_dir(vm) / paths.QGA_SOCKET
    deadline = time.monotonic() + timeout
    last = ""
    next_note = time.monotonic() + 20

    say(f"Waiting for cloud-init in {vm} (up to {int(timeout)}s)")
    while time.monotonic() < deadline:
        try:
            with qga.Qga(path, timeout=8.0) as client:
                client.execute("guest-ping")
                status = _guest_run(
                    client, ["/bin/sh", "-c", f"test -f {BOOT_FINISHED}"], timeout=8.0
                )
                if int(status.get("exitcode") or 0) == 0:
                    say(f"cloud-init finished in {vm}")
                    return True
                last = "boot-finished is not there yet"
        except FileNotFoundError:
            last = "the guest agent socket does not exist yet"
        except (QgaError, NotFound, OSError) as exc:
            last = str(exc)

        now = time.monotonic()
        if now >= next_note:
            say(f"  still waiting: {last}")
            next_note = now + 20
        time.sleep(POLL)

    raise CloudError(
        f"cloud-init did not finish in {vm} within {int(timeout)}s"
        + (f" (last: {last})" if last else "")
        + ". Check the serial console or the web console; the seed lives at "
        f"{paths.seed_dir(vm)}. Provisioning the rest over SSH may still work."
    )
