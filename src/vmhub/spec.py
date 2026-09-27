from __future__ import annotations

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


@dataclass
class Network:
    mode: str = "user"
    guest_ports: list[int] = field(default_factory=lambda: [22])
    host_web: int = 0
    host_ssh: int = 0
    host_rdp: int = 0
    bind: str = "127.0.0.1"
    mac: str = ""
    ip: str = ""

    def published(self) -> dict[int, int]:
        mapping: dict[int, int] = {}
        for host, guest in (
            (self.host_web, 8006),
            (self.host_ssh, 22),
            (self.host_rdp, 3389),
        ):
            if host:
                mapping[host] = guest
        return mapping


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
        for port in self.network.published():
            if not 1 <= port <= 65535:
                raise SpecError(f"published host port out of range: {port}")
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
        cleaned["BOOT"] = env["BOOT"]
        return cleaned


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value is False or value == 0


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _prune(val) if isinstance(val, dict) else val for key, val in data.items() if not _is_empty(val)}


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
    network = Network(
        **{
            k: v
            for k, v in (data.get("network") or {}).items()
            if k in {"mode", "guest_ports", "host_web", "host_ssh", "host_rdp", "bind", "mac", "ip"}
        }
    )
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
            if k in {"install", "drivers", "rdp_user", "rdp_password"}
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
    path.write_text(
        toml_io.dumps(spec.to_dict(), header=header or f"vmhub spec for {spec.name}")
    )
