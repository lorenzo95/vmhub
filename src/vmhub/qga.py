from __future__ import annotations

import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import NotFound, QgaError

DEFAULT_TIMEOUT = 6.0
SYNC_DELIMITER = b"\xff"
LOOPBACK_PREFIXES = ("127.", "::1")


@dataclass
class GuestInfo:
    reachable: bool
    hostname: str = ""
    os_name: str = ""
    ip: str = ""
    reason: str = ""

    def describe(self) -> str:
        if not self.reachable:
            return f"guest agent not answering ({self.reason})"
        bits = [f"ip {self.ip}"] if self.ip else []
        if self.hostname:
            bits.append(self.hostname)
        if self.os_name:
            bits.append(self.os_name)
        return ", ".join(bits) or "agent answered"


class Qga:
    """Client for the QEMU Guest Agent.

    The agent is bridged to a unix socket inside the container's /storage, so the
    host can speak to it directly. QGA's protocol needs a sync handshake first:
    the agent may have buffered output from before we connected, and
    guest-sync-delimited marks the boundary with a 0xFF byte.
    """

    def __init__(self, path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self) -> Qga:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> Qga:
        if not self.path.exists():
            raise NotFound(f"guest agent socket not present: {self.path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise QgaError(f"cannot reach the guest agent: {exc}") from exc
        self._sock = sock
        self._sync()
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _send(self, payload: dict[str, Any]) -> None:
        if self._sock is None:
            raise QgaError("guest agent not connected")
        self._sock.sendall((json.dumps(payload) + "\n").encode())

    def _fill(self, deadline: float) -> None:
        if self._sock is None:
            raise QgaError("guest agent not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QgaError("timed out talking to the guest agent")
        self._sock.settimeout(remaining)
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout as exc:
            raise QgaError("timed out talking to the guest agent") from exc
        if not chunk:
            raise QgaError("guest agent closed the connection")
        self._buf += chunk

    def _read_line(self, deadline: float) -> bytes:
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line, self._buf = self._buf[:newline], self._buf[newline + 1 :]
                if line.strip():
                    return line
                continue
            self._fill(deadline)

    def _sync(self) -> None:
        ident = secrets.randbelow(1 << 30) + 1
        self._send({"execute": "guest-sync-delimited", "arguments": {"id": ident}})
        deadline = time.monotonic() + self.timeout
        while True:
            while SYNC_DELIMITER not in self._buf:
                self._fill(deadline)
            _, _, self._buf = self._buf.partition(SYNC_DELIMITER)
            line = self._read_line(deadline)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("return") == ident:
                return

    def execute(self, command: str, **arguments: Any) -> Any:
        request: dict[str, Any] = {"execute": command}
        if arguments:
            request["arguments"] = arguments
        self._send(request)
        deadline = time.monotonic() + self.timeout
        while True:
            line = self._read_line(deadline)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in message:
                err = message["error"]
                raise QgaError(f"{command} failed: {err.get('desc', err)}")
            if "return" in message:
                return message["return"]


def is_loopback(address: str) -> bool:
    return address.startswith(LOOPBACK_PREFIXES)


def pick_ip(interfaces: list[dict[str, Any]]) -> str:
    """First non-loopback IPv4 the guest reports."""
    for iface in interfaces:
        for addr in iface.get("ip-addresses") or []:
            if addr.get("ip-address-type") != "ipv4":
                continue
            value = str(addr.get("ip-address") or "")
            if value and not is_loopback(value):
                return value
    return ""


def query(vm: str, *, timeout: float = DEFAULT_TIMEOUT) -> GuestInfo:
    """Ask the guest agent for identity, over a short socket conversation.

    Deliberately on-demand rather than part of the status poll: a guest without
    the agent installed would otherwise cost a timeout on every refresh.
    """
    from . import paths

    path = paths.storage_dir(vm) / "qga.sock"
    if not path.exists():
        return GuestInfo(False, reason="no agent socket (guest may not have the agent installed)")
    try:
        with Qga(path, timeout=timeout) as client:
            client.execute("guest-ping")
            hostname = ""
            os_name = ""
            try:
                osinfo = client.execute("guest-get-osinfo") or {}
                hostname = str(osinfo.get("hostname") or "")
                os_name = str(osinfo.get("pretty-name") or osinfo.get("name") or "")
            except QgaError:
                pass
            interfaces = client.execute("guest-network-get-interfaces") or []
    except (QgaError, NotFound, OSError) as exc:
        return GuestInfo(False, reason=str(exc))
    return GuestInfo(
        True,
        hostname=hostname,
        os_name=os_name,
        ip=pick_ip(interfaces if isinstance(interfaces, list) else []),
    )


def shutdown(vm: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Ask the guest to shut itself down. This is the only clean way to stop
    Windows, which does not answer the container's ACPI signal."""
    from . import paths

    path = paths.storage_dir(vm) / "qga.sock"
    with Qga(path, timeout=timeout) as client:
        client.execute("guest-shutdown", mode="powerdown")
