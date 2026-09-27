from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from .errors import NotFound, QmpError

DEFAULT_TIMEOUT = 15.0


class Qmp:
    def __init__(self, path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self) -> Qmp:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> Qmp:
        if not self.path.exists():
            raise NotFound(f"QMP socket not present: {self.path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise QmpError(f"cannot connect to QMP at {self.path}: {exc}") from exc
        self._sock = sock
        greeting = self._read_message()
        if "QMP" not in greeting:
            raise QmpError(f"unexpected QMP greeting: {greeting}")
        self.execute("qmp_capabilities")
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _read_message(self) -> dict[str, Any]:
        if self._sock is None:
            raise QmpError("QMP not connected")
        deadline = time.monotonic() + self.timeout
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line, self._buf = self._buf[:newline], self._buf[newline + 1 :]
                if not line.strip():
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError as exc:
                    raise QmpError(f"malformed QMP message: {line[:200]!r}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QmpError("timed out reading QMP message")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise QmpError("timed out reading QMP message") from exc
            if not chunk:
                raise QmpError("QMP connection closed by peer")
            self._buf += chunk

    def _send(self, payload: dict[str, Any]) -> None:
        if self._sock is None:
            raise QmpError("QMP not connected")
        self._sock.sendall((json.dumps(payload) + "\n").encode())

    def execute(self, command: str, **arguments: Any) -> Any:
        request: dict[str, Any] = {"execute": command}
        if arguments:
            request["arguments"] = arguments
        self._send(request)
        while True:
            message = self._read_message()
            if "event" in message:
                continue
            if "error" in message:
                err = message["error"]
                raise QmpError(f"{command} failed: {err.get('desc', err)}")
            if "return" in message:
                return message["return"]
            if "QMP" in message:
                continue
            raise QmpError(f"unexpected QMP reply to {command}: {message}")

    def query_status(self) -> str:
        result = self.execute("query-status")
        return str(result.get("status", "unknown"))

    def is_running(self) -> bool:
        return self.query_status() == "running"

    def block_backends(self) -> list[dict[str, Any]]:
        return self.execute("query-named-block-nodes") or []


    def quit(self) -> None:
        try:
            self.execute("quit")
        except QmpError:
            pass

    def take_snapshot(self, device: str, name: str) -> None:
        self.execute("blockdev-snapshot-internal-sync", device=device, name=name)

    def delete_snapshot(self, device: str, name: str) -> None:
        self.execute("blockdev-snapshot-delete-internal-sync", device=device, name=name)


def primary_block_device(qmp: Qmp, *, disk_name: str = "data.qcow2") -> str:
    nodes = qmp.block_backends()
    writable = [n for n in nodes if not n.get("ro")]
    for node in writable:
        if str(node.get("file", "")).endswith("/" + disk_name):
            name = node.get("node-name")
            if name:
                return str(name)
    for node in writable:
        name = str(node.get("node-name") or "")
        if name.startswith("data"):
            return name
    if writable:
        return str(writable[0].get("node-name") or "")
    if nodes:
        return str(nodes[0].get("node-name") or "")
    return "data3"
