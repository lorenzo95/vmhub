from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
import subprocess
from dataclasses import dataclass
from urllib.parse import quote

from . import registry
from .errors import VmhubError

REMMINA = "remmina"
FREERDP = "xfreerdp"

RDP_NEGOTIATE = bytes.fromhex("030000130ee000000000000100080003000000")
RDP_PROTOCOLS = {
    0: "0x0 - legacy, TLS must be negotiated first",
    1: "0x1 - RDP 4.x",
    2: "0x2 - RDP 5.0+ (TLS + NLA)",
    3: "0x3 - RDP 5.0+/NLA with CredSSP",
}


@dataclass
class RdpTarget:
    host: str
    port: int
    user: str
    password: str
    resolution: str = "1920x1080"

    def __post_init__(self) -> None:
        # A default password only makes sense next to a known account; with no
        # user configured the client should prompt for both.
        if not self.user:
            self.password = ""

    @property
    def available(self) -> bool:
        return self.port > 0

    def uri(self, *, include_password: bool = False) -> str:
        """Build a remmina URI.

        The password is omitted by default: remmina prompts for it, and a
        password in the URI would land in the process table, remmina's recent
        list, and any bundle manifest that embeds this string.
        """
        user = quote(self.user, safe="")
        if include_password and self.password and user:
            auth = f"{user}:{quote(self.password, safe='')}@"
        elif user:
            auth = f"{user}@"
        else:
            auth = ""
        return f"rdp://{auth}{self.host}:{self.port}/?resolution={self.resolution}&viewmode=1"

    def freerdp_argv(self) -> list[str]:
        argv = [FREERDP, f"/v:{self.host}:{self.port}"]
        if self.user:
            argv.append(f"/u:{self.user}")
        if self.password:
            argv.append(f"/p:{self.password}")
        argv += [
            "/cert:ignore",
            "/scale:100",
            f"/w:{self.resolution.split('x')[0]}",
            f"/h:{self.resolution.split('x')[-1]}",
        ]
        return argv


def rdp_user(vm: str) -> str:
    """The configured account, or "" when the guest's account name is unknown.

    There is no unattended install, so vmhub cannot know the account; guessing
    one produces a URI that looks authoritative but cannot work.
    """
    vm_spec = registry.load_spec(vm)
    if vm_spec.media.rdp_user:
        return vm_spec.media.rdp_user
    try:
        from . import blueprints

        return blueprints.load(vm_spec.blueprint).rdp_user or ""
    except VmhubError:
        return ""


def target(vm: str) -> RdpTarget:
    vm_spec = registry.load_spec(vm)
    return RdpTarget(
        host=vm_spec.network.bind or "127.0.0.1",
        port=vm_spec.network.host_rdp,
        user=rdp_user(vm),
        password=vm_spec.media.rdp_password or "admin",
    )


def probe(vm: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Check the whole path host -> container -> guest by speaking RDP to it.

    Distinguishes "the port is not published" from "the guest is not listening",
    which otherwise look identical from the client.
    """
    tgt = target(vm)
    if not tgt.available:
        return False, "no RDP port is published for this VM"
    try:
        with socket.create_connection((tgt.host, tgt.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(RDP_NEGOTIATE)
            reply = sock.recv(19)
    except ConnectionRefusedError:
        return False, f"nothing is listening on {tgt.host}:{tgt.port} — is the VM running?"
    except socket.timeout:
        return False, (
            f"{tgt.host}:{tgt.port} accepted the connection but never answered — "
            f"the port is published but the guest is not answering on 3389"
        )
    except OSError as exc:
        return False, f"cannot reach {tgt.host}:{tgt.port}: {exc}"
    if reply[:2] != b"\x03\x00" or len(reply) < 19:
        return True, f"something answered on {tgt.host}:{tgt.port} but not as RDP"
    selected = int.from_bytes(reply[15:19], "little")
    negotiated = RDP_PROTOCOLS.get(selected, hex(selected))
    detail = f"guest negotiated {negotiated} on {tgt.host}:{tgt.port}"
    if selected in (2, 3):
        detail += (
            ". NLA is on, so the client must supply valid Windows credentials "
            "before the session opens - a wrong or missing account is the usual cause."
        )
    return True, detail


PROFILE_GROUP = "vmhub"
PROFILE_FIELDS = {
    "resolution_width": 1920,
    "resolution_height": 1080,
    "resolution_mode": 0,
    "colordepth": 63,
    "viewmode": 1,
    "cert_ignore": 1,
    "ignore-tls-errors": 1,
    "security": "",
    "network": "none",
    "disableclipboard": 0,
    "disablepasswordstoring": 0,
    "disable_fastpath": 0,
    "multitransport": 0,
    "gateway_usage": 0,
    # Remmina writes both the legacy and current spellings of these two keys, so
    # match it: whichever its version reads, one of them is there.
    "restrictedadmin": 0,
    "restricted-admin": 0,
    "domain": "",
    "gateway_host": "",
    "gateway_server": "",
    "gateway_username": "",
    "gateway_password": "",
}


def profile_path(vm: str) -> Path:
    return Path.home() / ".local/share/remmina" / f"{PROFILE_GROUP}-{vm}.remmina"


def write_profile(vm: str) -> Path:  # noqa: D401
    """Write a native .remmina profile.

    Remmina's URI parser drops the server, so `remmina -c rdp://...` reaches
    FreeRDP with a NULL address ("could not find the address (null)").

    The port must be part of the `server` value, not only its own `port` key:
    Remmina writes `server=host:port` *and* `port=port` in its own profiles, and
    a profile with only `port=` set silently connects to 3389 instead.
    """
    tgt = target(vm)
    if not tgt.available:
        raise VmhubError(
            f"{vm} has no published RDP port. Add one with: "
            f"vmctl set {vm} network.guest_ports=3389"
        )
    path = profile_path(vm)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[remmina]",
        f"name={vm}",
        f"server={tgt.host}:{tgt.port}",
        f"port={tgt.port}",
        "protocol=RDP",
        f"username={tgt.user}",
        "password=",
        f"group={PROFILE_GROUP}",
    ]
    lines += [f"{key}={value}" for key, value in PROFILE_FIELDS.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def remove_profile(vm: str) -> bool:
    path = profile_path(vm)
    if path.is_file():
        path.unlink()
        return True
    return False


def remmina_available() -> bool:
    return shutil.which(REMMINA) is not None


def freerdp_available() -> bool:
    return shutil.which(FREERDP) is not None


def command(vm: str) -> tuple[str, list[str]] | None:
    tgt = target(vm)
    if not tgt.available:
        return None
    if remmina_available():
        return REMMINA, [REMMINA, "-c", str(profile_path(vm))]
    if freerdp_available():
        return FREERDP, tgt.freerdp_argv()
    return None


def launch(vm: str) -> str:
    chosen = command(vm)
    if chosen is None:
        tgt = target(vm)
        if not tgt.available:
            raise VmhubError(
                f"{vm} has no published RDP port. Add 3389 to network.guest_ports "
                f"in its spec, for example: vmctl set {vm} network.guest_ports=3389"
            )
        raise VmhubError(
            f"neither {REMMINA} nor {FREERDP} is installed. "
            f"You can connect manually with Remmina to {target(vm).host}:{target(vm).port}"
        )
    if chosen[0] == REMMINA:
        write_profile(vm)
    name, argv = chosen
    if os.environ.get("VMHUB_NO_LAUNCH"):
        return f"would launch {name} for {target(vm).host}:{target(vm).port}"
    subprocess.Popen(argv, start_new_session=True)
    return f"launched {name} for {target(vm).host}:{target(vm).port}"
