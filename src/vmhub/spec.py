from __future__ import annotations

import os
import re
import uuid as uuidlib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths, toml_io
from .errors import SpecError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
BOOT_MODES = {
    "auto",
    "uefi",
    "secure",
    "legacy",
    "windows",
    "windows_plain",
    "windows_secure",
    "windows_legacy",
}
DISK_TYPES = {"scsi", "virtio-scsi", "blk", "virtio-blk", "ide", "sata", "nvme", "usb"}
DISK_FMTS = {"qcow2", "raw"}
NET_MODES = {"user", "N"}
EXPORT_FORMATS = ("qcow2", "raw", "vmdk", "vdi", "vhdx", "vpc")
DEFAULT_REPO = "docker.io/qemux/qemu"

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)(?:i?B)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(text: str) -> int:
    match = _SIZE_RE.match(str(text))
    if not match:
        raise SpecError(f"invalid size: {text!r} (try 8G, 512M, 64G)")
    value, unit = match.groups()
    size = int(float(value) * _UNITS[unit.upper()])
    if size <= 0:
        raise SpecError(f"size must be positive: {text!r}")
    return size


def format_size(num: int) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if num < 1024 or unit == "P":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}".replace(".0", "")
        num /= 1024.0
    return f"{num:.1f}P"


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def new_mac() -> str:
    return "52:54:00:%02x:%02x:%02x" % (
        uuidlib.uuid4().bytes[0],
        uuidlib.uuid4().bytes[1],
        uuidlib.uuid4().bytes[2],
    )


def validate_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise SpecError(
            f"invalid VM name: {name!r} — use letters, digits, '-' or '_' (max 63)"
        )
    return name


@dataclass
class Image:
    repository: str = DEFAULT_REPO
    tag: str = "latest"

    @property
    def ref(self) -> str:
        if "@" in self.repository:
            return self.repository
        return f"{self.repository}:{self.tag}"


def split_image_ref(image: str) -> tuple[str, str]:
    """Split an image reference into (repository, tag).

    Only a colon after the last slash is a tag separator. A registry
    carries a port, so `registry.example:5000/qemu` is one repository and
    no tag -- splitting on the first colon turns it into the repository
    `registry.example` and the tag `5000/qemu`, which is not an image
    anybody can pull.

    A digest reference is already pinned, so it is all repository and no
    tag, which is what Image.ref then reproduces exactly.
    """
    image = image.strip()
    if "@" in image:
        return image, ""
    head, sep, tail = image.rpartition(":")
    if not sep or "/" in tail:
        return image, ""
    return head, tail


@dataclass
class Boot:
    mode: str = "uefi"
    iso: str = ""
    media_type: str = ""


@dataclass
class Resources:
    cpus: int = 2
    ram: str = "4G"
    disk: str = "64G"
    disk_type: str = "scsi"
    disk_fmt: str = "qcow2"
    kvm: bool = True
    ram_check: bool = True

    @property
    def disk_bytes(self) -> int:
        return parse_size(self.disk)


PORT_ROLES = ("web", "ssh", "rdp")
PORT_KEYS = {"guest", "host", "role", "name"}


@dataclass
class Port:
    """One published host port.

    `role` names the three ports vmhub drives itself: the container's own
    web console, SSH and RDP. A port added for the user's own guest service
    has no role. A `host` of 0 keeps the entry but does not publish it, so
    the same guest port comes back the next time the VM is started.
    """

    guest: int = 0
    host: int = 0
    role: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.role or f"guest {self.guest}"


@dataclass
class Network:
    mode: str = "user"
    guest_ports: list[int] = field(default_factory=lambda: [22])
    ports: list[Port] = field(default_factory=list)
    bind: str = "127.0.0.1"
    mac: str = ""
    ip: str = ""

    def published(self) -> dict[int, int]:
        return {port.host: port.guest for port in self.ports if port.host}

    def by_role(self, role: str) -> Port | None:
        for port in self.ports:
            if port.role == role:
                return port
        return None

    def host_for(self, role: str) -> int:
        port = self.by_role(role)
        return port.host if port else 0

    def set_role(self, role: str, guest: int, host: int) -> None:
        existing = self.by_role(role)
        if existing is None:
            self.ports.append(Port(guest=guest, host=host, role=role))
        else:
            existing.guest = guest
            existing.host = host


@dataclass
class Display:
    protected: bool = False
    password: str = ""
    audio: bool = False
    gpu: bool = False
    lossy: bool = False
    vga: str = "virtio"


@dataclass
class Shutdown:
    timeout: int = 30
    skip_acpi: bool = False

    @property
    def podman_grace(self) -> int:
        return self.timeout + 20


@dataclass
class Media:
    install: str = ""
    drivers: str = ""
    rdp_user: str = ""
    rdp_password: str = ""
    # A folder mapped into this VM's RDP session, overriding the app-wide
    # default in settings.toml. Empty means "use the default", not "map
    # nothing" -- mapping nothing is what an empty default means.
    share_path: str = ""

    def media_path(self, slot: str) -> str:
        if slot == "install":
            return self.install
        if slot == "drivers":
            return self.drivers
        raise SpecError(f"unknown media slot: {slot}")


@dataclass
class Features:
    hv: bool = True
    ballooning: bool = False
    guest_agent: bool = True
    tpm: bool = False
    host_share: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class VmSpec:
    name: str
    blueprint: str = ""
    image: Image = field(default_factory=Image)
    boot: Boot = field(default_factory=Boot)
    resources: Resources = field(default_factory=Resources)
    network: Network = field(default_factory=Network)
    display: Display = field(default_factory=Display)
    media: Media = field(default_factory=Media)
    shutdown: Shutdown = field(default_factory=Shutdown)
    features: Features = field(default_factory=Features)

    def validate(self) -> VmSpec:
        validate_name(self.name)
        if self.boot.mode not in BOOT_MODES:
            raise SpecError(f"boot.mode must be one of {sorted(BOOT_MODES)}")
        if self.resources.disk_type not in DISK_TYPES:
            raise SpecError(f"resources.disk_type must be one of {sorted(DISK_TYPES)}")
        if self.resources.disk_fmt not in DISK_FMTS:
            raise SpecError(f"resources.disk_fmt must be one of {sorted(DISK_FMTS)}")
        if self.network.mode not in NET_MODES:
            raise SpecError(f"network.mode must be one of {sorted(NET_MODES)}")
        if not 1 <= self.resources.cpus <= 512:
            raise SpecError("resources.cpus out of range")
        parse_size(self.resources.ram)
        parse_size(self.resources.disk)
        for port in self.network.guest_ports:
            if not 1 <= int(port) <= 65535:
                raise SpecError(f"network.guest_ports: bad port {port}")
        seen_hosts: dict[int, str] = {}
        seen_roles: set[str] = set()
        for port in self.network.ports:
            if not 1 <= int(port.guest) <= 65535:
                raise SpecError(f"network.ports: guest port out of range: {port.guest}")
            if port.host and not 1 <= int(port.host) <= 65535:
                raise SpecError(f"network.ports: host port out of range: {port.host}")
            if port.role and port.role not in PORT_ROLES:
                raise SpecError(
                    f"network.ports: {port.label} has unknown role {port.role!r}; "
                    f"expected one of {list(PORT_ROLES)}, or none"
                )
            if port.role:
                if port.role in seen_roles:
                    raise SpecError(
                        f"network.ports: two entries claim the {port.role} role -- "
                        "only one of them can be the port vmctl uses for it"
                    )
                seen_roles.add(port.role)
            if port.host and port.host in seen_hosts:
                raise SpecError(
                    f"network.ports: host port {port.host} is published twice "
                    f"({seen_hosts[port.host]} and {port.label})"
                )
            if port.host:
                seen_hosts[port.host] = port.label
        if self.display.protected and not self.display.password:
            raise SpecError("display.protected requires display.password")
        if not 0 <= int(self.shutdown.timeout) <= 600:
            raise SpecError("shutdown.timeout must be between 0 and 600 seconds")
        return self

    def media_path(self, slot: str) -> str:
        return self.media.media_path(slot)

    def to_dict(self) -> dict[str, Any]:
        return _prune({k: v for k, v in asdict(self).items() if k != "name"})

    def env(self, *, uuid: str, mac: str) -> dict[str, str]:
        env: dict[str, str] = {
            "DISK_FMT": self.resources.disk_fmt,
            "DISK_TYPE": self.resources.disk_type,
            "DISK_NAME": "data",
            "CPU_CORES": str(self.resources.cpus),
            "RAM_SIZE": self.resources.ram,
            # "auto" leaves BOOT_MODE unset so the container inspects the disk
            # and picks firmware itself, which is what an imported disk needs.
            "BOOT_MODE": "" if self.boot.mode == "auto" else self.boot.mode,
            "KVM": "Y" if self.resources.kvm else "N",
            "RAM_CHECK": "Y" if self.resources.ram_check else "N",
            "NETWORK": self.network.mode,
            "USER_PORTS": ",".join(str(p) for p in sorted(set(self.network.guest_ports))),
            "QMP": f"/storage/{paths.QMP_SOCKET}",
            "QGA": f"/storage/{paths.QGA_SOCKET}" if self.features.guest_agent else "",
            "MONITOR": f"/storage/{paths.MONITOR_SOCKET}",
            "UUID": uuid,
            "MAC": mac or self.network.mac,
            "DISPLAY": "web",
            "WEB": "Y",
            "WEB_PORT": "8006",
            "SHUTDOWN": "N" if self.shutdown.skip_acpi else "Y",
            "TIMEOUT": str(int(self.shutdown.timeout)),
            "HV": "Y" if self.features.hv else "N",
            "AUDIO": "Y" if self.display.audio else "N",
            "GPU": "Y" if self.display.gpu else "N",
            "LOSSY": "Y" if self.display.lossy else "N",
            "PROTECT": "Y" if self.display.protected else "N",
            "BALLOONING": "Y" if self.features.ballooning else "N",
            "TPM": "Y" if self.features.tpm else "N",
            "ALLOCATE": "N",
        }
        env["BOOT"] = self.boot.iso
        if self.boot.media_type:
            env["MEDIA_TYPE"] = self.boot.media_type
        if self.network.ip:
            env["IP"] = self.network.ip
        if self.display.password:
            env["PASSWORD"] = self.display.password
        env.update(self.features.extra_env)
        cleaned = {k: v for k, v in env.items() if v != ""}
        # Keep BOOT present even when empty. The container treats an
        # absent or empty BOOT as "unspecified" and downloads its own
        # Alpine image, so this is the same either way -- but sending it
        # explicitly documents that "no boot media" is a real state here
        # rather than an oversight. It is not a way to suppress the
        # fallback: BOOT=none is rejected by the container.
        cleaned["BOOT"] = env["BOOT"]
        return cleaned


def _is_empty(value: Any) -> bool:
    """True for values worth omitting from vm.toml.

    False and 0 are deliberately NOT empty. Dropping them here meant a
    spec that had guest_agent=False or shutdown.timeout=0 came back from
    spec_from_dict() holding the dataclass default instead, and `vmctl set`
    reported success while the value silently reverted. Writing the value
    is the only way the file can distinguish 'explicitly off' from 'never
    set', so False and 0 are kept even where they match the default.
    """
    return value is None or value == "" or value == [] or value == {}


def _prune_items(value: Any) -> Any:
    """Prune inside a list of tables too, so a port entry only writes the
    fields it actually uses. The list itself is kept whole."""
    if isinstance(value, list):
        return [_prune(item) if isinstance(item, dict) else item for item in value]
    return value


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _prune(val) if isinstance(val, dict) else _prune_items(val)
        for key, val in data.items()
        if not _is_empty(val)
    }


def _legacy_ports(net_data: dict) -> list[Port]:
    """Fold the three fixed host_* slots into port entries.

    A spec written before the list existed still says which ports it wants;
    read them so an existing VM keeps working, and let the next save write
    the list instead. A host_* of 0 meant "not published" and still does.
    """
    out: list[Port] = []
    for role, key, guest in (
        ("web", "host_web", 8006),
        ("ssh", "host_ssh", 22),
        ("rdp", "host_rdp", 3389),
    ):
        try:
            host = int(net_data.get(key) or 0)
        except (TypeError, ValueError):
            host = 0
        if host:
            out.append(Port(guest=guest, host=host, role=role))
    return out


def spec_from_dict(data: dict, *, name: str | None = None) -> VmSpec:
    name = data.get("name", "") if name is None else name
    blueprint = data.get("blueprint", "")
    image = Image(**{k: v for k, v in (data.get("image") or {}).items() if k in {"repository", "tag"}})
    boot = Boot(**{k: v for k, v in (data.get("boot") or {}).items() if k in {"mode", "iso", "media_type"}})
    resources = Resources(
        **{
            k: v
            for k, v in (data.get("resources") or {}).items()
            if k in {"cpus", "ram", "disk", "disk_type", "disk_fmt", "kvm", "ram_check"}
        }
    )
    net_data = data.get("network") or {}
    network = Network(
        **{k: v for k, v in net_data.items() if k in {"mode", "guest_ports", "bind", "mac", "ip"}}
    )
    network.ports = [
        Port(**{k: v for k, v in entry.items() if k in PORT_KEYS})
        for entry in (net_data.get("ports") or [])
    ]
    if not network.ports:
        network.ports = _legacy_ports(net_data)
    display = Display(
        **{
            k: v
            for k, v in (data.get("display") or {}).items()
            if k in {"protected", "password", "audio", "gpu", "lossy", "vga"}
        }
    )
    media = Media(
        **{
            k: v
            for k, v in (data.get("media") or {}).items()
            if k in {"install", "drivers", "rdp_user", "rdp_password", "share_path"}
        }
    )
    shutdown = Shutdown(
        **{
            k: v
            for k, v in (data.get("shutdown") or {}).items()
            if k in {"timeout", "skip_acpi"}
        }
    )
    features = Features(
        **{
            k: v
            for k, v in (data.get("features") or {}).items()
            if k in {"hv", "ballooning", "guest_agent", "tpm", "host_share", "extra_env"}
        }
    )
    return VmSpec(
        name=name,
        blueprint=blueprint,
        image=image,
        boot=boot,
        resources=resources,
        network=network,
        display=display,
        media=media,
        shutdown=shutdown,
        features=features,
    )


def load(path: Path, *, name: str | None = None) -> VmSpec:
    """Load a spec. `name` overrides whatever the file claims, because the
    directory it lives in is the VM's real identity."""
    if not path.is_file():
        raise SpecError(f"no such spec: {path}")
    return spec_from_dict(toml_io.loads(path.read_text()), name=name).validate()


def save(spec: VmSpec, path: Path, *, header: str = "") -> None:
    spec.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = toml_io.dumps(
        spec.to_dict(), header=header or f"vmhub spec for {spec.name}"
    )
    # Write beside the target, then rename over it. A plain write_text leaves
    # the file truncated and half-written if anything interrupts it -- another
    # writer, a crash, a full disk -- and a vm.toml that parses as neither the
    # old spec nor the new one makes the VM unreadable rather than merely
    # stale. os.replace is atomic within a filesystem, so a reader sees the
    # old file or the new one and never a splice of both.
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(body)
        os.replace(tmp, path)
    finally:
        # A failed rename leaves the scratch file behind; it is not the spec.
        tmp.unlink(missing_ok=True)
