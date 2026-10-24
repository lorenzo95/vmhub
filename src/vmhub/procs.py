"""The host processes a VM owns, and the ones podman lost track of.

podman owns a VM's container, but what runs inside it does not belong to
podman: QEMU, the web console and the port forwarder podman runs for the
container's published ports are ordinary host processes. They live for
exactly as long as podman keeps the container's record, and when podman
loses that record -- a storage reset, a wiped database, a container
removed while its processes were still running -- the processes carry on
with nothing left to manage them.

The result is a VM that vmhub calls "stopped" while its QEMU is still
running, still holding the disk open and still holding the host ports
podman published for it. Starting it then fails with a port conflict
against its own leftovers, and stopping it does nothing at all, because
podman has no container to stop. This module is how those processes are
found again, and this is how each one is tied back to its VM:

    QEMU       the -uuid on its command line is the VM's own UUID
    conmon     the -n on its command line is the container's name
    forwarder  the network namespace it was handed is the container's
               own, so the inode of that namespace identifies it exactly

The forwarder is the one that needs care. Podman runs pasta on the host
to publish the container's ports, and pasta is given the container's
network namespace as an argument -- a path, which podman deletes when the
container goes away. The namespace itself survives as long as somebody
holds it open, and its inode is the same number the container's own
processes see, so the two are matched by number rather than by name.
"""

from __future__ import annotations

import os
import signal
import time

from dataclasses import dataclass, field

from pathlib import Path
from typing import Iterable

PROC = Path("/proc")

# How long a process is given to go away on SIGTERM before it is killed.
TERM_TIMEOUT = 5.0
# How long it is given to go away after SIGKILL. It is gone or it never
# will be; this is only here so the caller does not have to be told.
KILL_TIMEOUT = 3.0

# The names these processes go by. pasta names itself pasta.avx2 on some
# builds; the passt *inside* the container is a different program and must
# not be mistaken for the forwarder outside it.
QEMU_PREFIX = "qemu-system"
FORWARDER_PREFIX = "pasta"
CONMON_NAME = "conmon"


@dataclass(frozen=True)
class Qemu:
    """A QEMU process, and the VM it belongs to."""

    pid: int
    uuid: str
    netns: int = 0


@dataclass(frozen=True)
class Forwarder:
    """The host-side port forwarder podman runs for a container."""

    pid: int
    host_ports: tuple[int, ...] = ()
    netns: int = 0


@dataclass(frozen=True)
class Conmon:
    """The container's supervisor."""

    pid: int
    container: str


@dataclass
class Leftovers:
    """What is still running for a VM that podman has no container for.

    Any of the three can be missing on its own: a container whose QEMU
    died leaves its forwarder holding the ports, and a container that
    never started leaves nothing at all.
    """

    qemu: Qemu | None = None
    forwarder: Forwarder | None = None
    conmon: Conmon | None = None

    @property
    def running(self) -> bool:
        return bool(self.qemu or self.forwarder or self.conmon)

    @property
    def pids(self) -> list[int]:
        return [p.pid for p in (self.qemu, self.forwarder, self.conmon) if p]

    @property
    def held_ports(self) -> list[int]:
        """The host ports a leftover forwarder is still holding."""
        return list(self.forwarder.host_ports) if self.forwarder else []

    def describe(self) -> str:
        """One line saying what is still running."""
        bits = []
        if self.qemu:
            bits.append(f"QEMU pid {self.qemu.pid}")
        if self.forwarder:
            bits.append(f"port forwarder pid {self.forwarder.pid}")
        if self.conmon:
            bits.append(f"conmon pid {self.conmon.pid}")
        return ", ".join(bits)


@dataclass
class Snapshot:
    """Every QEMU, port forwarder and conmon on the host, in one pass.

    Taken once per refresh rather than once per VM: reading every
    /proc/<pid>/cmdline costs a few milliseconds, and a refresh that
    does it seven times over is a refresh that takes half a second.
    """

    qemus: dict[str, Qemu] = field(default_factory=dict)
    forwarders: list[Forwarder] = field(default_factory=list)
    conmons: dict[str, Conmon] = field(default_factory=dict)

    def qemu(self, uuid: str) -> Qemu | None:
        return self.qemus.get(uuid)


def _cmdline(pid: int) -> list[str]:
    """The argument list of a process, as the kernel sees it."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _flag_value(argv: list[str], flag: str) -> str:
    """The value of a command-line flag, in either spelling.

    QEMU writes "-uuid <value>", conmon writes "-n <value>", and both
    accept the joined form as well, so both are looked for.
    """
    joined = flag + "="
    for index, arg in enumerate(argv):
        if arg == flag:
            return argv[index + 1] if index + 1 < len(argv) else ""
        if arg.startswith(joined):
            return arg[len(joined) :]
    return ""


def _forwarded_ports(argv: list[str]) -> tuple[int, ...]:
    """The host ports a forwarder publishes.

    pasta is told them as -t 127.0.0.1/8006-8006:8006-8006: the host
    address and port first, the guest port after the colon.
    """
    ports: list[int] = []
    for index, arg in enumerate(argv):
        if arg == "-t":
            value = argv[index + 1] if index + 1 < len(argv) else ""
        elif arg.startswith("-t") and not arg.startswith("--"):
            value = arg[2:]
        else:
            continue
        host = value.split(":", 1)[0]
        number = host.rsplit("/", 1)[-1].split("-")[0]
        try:
            ports.append(int(number))
        except ValueError:
            continue
    return tuple(ports)


def _netns_inode(pid: int) -> int:
    """The inode of a process's own network namespace. 0 when unreadable."""
    try:
        return os.stat(f"/proc/{pid}/ns/net").st_ino
    except OSError:
        return 0


def _joined_netns(pid: int) -> int:
    """The inode of the network namespace a forwarder was handed.

    pasta does not join the namespace it configures: podman creates the
    container's namespace, passes the path to pasta, and pasta opens it.
    The open descriptor is what keeps the namespace alive once podman has
    deleted the path, and its inode is the number the container's own
    processes see.
    """
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if "/netns/" not in target:
            continue
        try:
            return os.stat(entry).st_ino
        except OSError:
            continue
    return 0


def _pids() -> list[int]:
    """Every process id on the host."""
    try:
        entries = os.scandir(PROC)
    except OSError:
        return []
    out = []
    with entries:
        for entry in entries:
            if entry.name.isdigit():
                out.append(int(entry.name))
    return out


def scan() -> Snapshot:
    """One pass over /proc: every QEMU, forwarder and conmon on the host."""
    snapshot = Snapshot()
    for pid in _pids():
        argv = _cmdline(pid)
        if not argv:
            continue
        name = Path(argv[0]).name
        if name.startswith(QEMU_PREFIX):
            uuid = _flag_value(argv, "-uuid")
            if not uuid:
                continue
            snapshot.qemus.setdefault(
                uuid, Qemu(pid=pid, uuid=uuid, netns=_netns_inode(pid))
            )
        elif name.startswith(FORWARDER_PREFIX):
            snapshot.forwarders.append(
                Forwarder(
                    pid=pid,
                    host_ports=_forwarded_ports(argv),
                    netns=_joined_netns(pid),
                )
            )
        elif name == CONMON_NAME:
            container = _flag_value(argv, "-n")
            if container:
                snapshot.conmons.setdefault(
                    container, Conmon(pid=pid, container=container)
                )
    return snapshot


def leftovers(
    snapshot: Snapshot,
    *,
    uuid: str,
    container: str,
    host_ports: Iterable[int] = (),
) -> Leftovers:
    """The processes still running for a VM that podman has lost.

    The forwarder is matched by the network namespace it shares with the
    VM's QEMU, which is exact. The ports it publishes are the fallback,
    for the case where the QEMU is already gone and only the forwarder
    is left holding the ports.

    The fallback matches the whole set of ports and not just one of
    them: a forwarder that happens to hold the same number as one of
    this VM's ports is more likely to belong to a different VM that
    used that number earlier.
    """
    qemu = snapshot.qemus.get(uuid)
    forwarder = None
    for candidate in snapshot.forwarders:
        if qemu and candidate.netns and candidate.netns == qemu.netns:
            forwarder = candidate
            break
    if forwarder is None:
        wanted = {int(p) for p in host_ports}
        for candidate in snapshot.forwarders:
            if wanted and set(candidate.host_ports) == wanted:
                forwarder = candidate
                break
    return Leftovers(
        qemu=qemu,
        forwarder=forwarder,
        conmon=snapshot.conmons.get(container),
    )


def alive(pid: int) -> bool:
    """Whether a process is still running."""
    return Path(f"/proc/{pid}").exists()


def signal_pid(pid: int, sig: int) -> bool:
    """Send a signal. False when the process is already gone."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def _ppid(pid: int) -> int:
    """The parent of a process, read from its stat line.

    The command name is in parentheses and may contain spaces, so the
    fields are counted from the last closing parenthesis.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0
    tail = data.rpartition(")")[2].split()
    try:
        return int(tail[1])
    except (IndexError, ValueError):
        return 0


def _children_map() -> dict[int, list[int]]:
    """Every process on the host, by parent."""
    children: dict[int, list[int]] = {}
    for pid in _pids():
        parent = _ppid(pid)
        if parent:
            children.setdefault(parent, []).append(pid)
    return children


def children_of(pid: int) -> list[int]:
    """The direct children of a process.

    For a container that is the init: conmon starts it, and it starts
    the entry script that starts QEMU.
    """
    return _children_map().get(pid, [])


def process_tree(root: int) -> list[int]:
    """Every descendant of a process, parents before children.

    The container's processes hang off conmon: conmon starts the
    container's init, which starts the entry script, which starts QEMU
    and the rest. Stopping the tree leaves nothing behind.
    """
    children = _children_map()
    out: list[int] = []
    pending = [root]
    while pending:
        current = pending.pop()
        for child in children.get(current, []):
            out.append(child)
            pending.append(child)
    return out


def terminate(pid: int, *, timeout: float = TERM_TIMEOUT) -> bool:
    """Ask a process to stop, then insist. True when it is gone."""
    if not alive(pid):
        return True
    signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    signal_pid(pid, signal.SIGKILL)
    deadline = time.monotonic() + KILL_TIMEOUT
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


def kill_now(pid: int, *, timeout: float = KILL_TIMEOUT) -> bool:
    """Kill a process outright. For the times when asking is not the point."""
    if not alive(pid):
        return True
    signal_pid(pid, signal.SIGKILL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


def still_matches(pid: int, *, flag: str, value: str) -> bool:
    """Whether a process is still the one it was found as.

    A pid is reused. Between finding a leftover and stopping it, the
    process may have died and its number may now belong to something
    else entirely -- so every process is checked against the command
    line it was found with before it is signalled.
    """
    return _flag_value(_cmdline(pid), flag) == value
