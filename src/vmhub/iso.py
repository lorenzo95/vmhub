from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import DiskError, NotFound

SLOTS = {
    "install": "/start.iso",
    "drivers": "/drivers.iso",
}
INSTALL_SLOT = "install"
DRIVERS_SLOT = "drivers"
SLOT_LABELS = {
    "install": "Install media (boots first)",
    "drivers": "Drivers (virtio etc.)",
}

SECTOR = 2048
SYSTEM_AREA_END = 16
PVD_LABEL_OFFSET = 0x27
PVD_LABEL_LENGTH = 32
BOOT_CATALOG_OFFSET = 39
BOOT_CATALOG_SECTORS = range(16, 40)
EL_TORITO_SIG = b"\x55\xaa"
ISO_MIN_BYTES = 48 * 1024
MAX_CATALOG_SECTOR = 1_000_000


@dataclass
class IsoInfo:
    path: str
    name: str
    size: int
    label: str
    is_iso: bool
    bootable: bool | None
    reason: str = ""

    @property
    def human_size(self) -> str:
        value = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f}{unit}".replace(".0", "")
            value /= 1024
        return f"{value:.1f}TB"

    def describe(self) -> str:
        if not self.is_iso:
            return f"{self.human_size} — not an ISO ({self.reason})"
        bits = [self.human_size]
        if self.label:
            bits.append(f'label "{self.label}"')
        if self.bootable is True:
            bits.append("bootable")
        return ", ".join(bits)


def _label(handle) -> str:
    handle.seek(SYSTEM_AREA_END * SECTOR + PVD_LABEL_OFFSET)
    return handle.read(PVD_LABEL_LENGTH).decode("ascii", "ignore").strip()


def _boot_catalog_sector(handle) -> int:
    for sector in BOOT_CATALOG_SECTORS:
        handle.seek(sector * SECTOR)
        head = handle.read(BOOT_CATALOG_OFFSET + 4)
        if len(head) < BOOT_CATALOG_OFFSET + 4:
            break
        if head[0] == 0 and head[1:6] == b"CD001":
            value = int.from_bytes(head[BOOT_CATALOG_OFFSET : BOOT_CATALOG_OFFSET + 4], "little")
            if 0 < value < MAX_CATALOG_SECTOR:
                return value
    return 0


def _el_torito(handle) -> bool:
    sector = _boot_catalog_sector(handle)
    if not sector:
        return False
    handle.seek(sector * SECTOR)
    entry = handle.read(32)
    if len(entry) < 32:
        return False
    return entry[0:2] == EL_TORITO_SIG and entry[30:32] == EL_TORITO_SIG


def _hybrid_mbr(handle) -> bool:
    handle.seek(0)
    mbr = handle.read(512)
    if len(mbr) < 512 or mbr[510:512] != EL_TORITO_SIG:
        return False
    return any(mbr[446 + i * 16 : 446 + (i + 1) * 16][4] for i in range(4))


def probe(path: Path) -> IsoInfo:
    path = Path(path).expanduser()
    name = path.name
    if not path.is_file():
        return IsoInfo(str(path), name, 0, "", False, None, "file does not exist")
    size = path.stat().st_size
    if size < ISO_MIN_BYTES:
        return IsoInfo(str(path), name, size, "", False, None, "file is too small to be an ISO")

    try:
        with open(path, "rb") as handle:
            handle.seek(SYSTEM_AREA_END * SECTOR + 1)
            iso9660 = handle.read(5) == b"CD001"
            handle.seek(SYSTEM_AREA_END * SECTOR + 1)
            udf = handle.read(5) in (b"NSR02", b"NSR03")
            if not (iso9660 or udf):
                return IsoInfo(
                    str(path), name, size, "", False, None,
                    "no ISO 9660 or UDF volume descriptor",
                )
            label = _label(handle) if iso9660 else ""
            bootable: bool | None = None
            if iso9660:
                bootable = _el_torito(handle) or _hybrid_mbr(handle) or None
    except OSError as exc:
        return IsoInfo(str(path), name, size, "", False, None, f"cannot read: {exc}")

    return IsoInfo(str(path), name, size, label, True, bootable)


def validate(path: Path) -> IsoInfo:
    info = probe(path)
    if not info.is_iso:
        raise DiskError(
            f"{Path(path).name} is not a usable ISO image: {info.reason}. "
            f"Expected an .iso or .img containing an ISO 9660 or UDF volume descriptor."
        )
    return info


def slot_path(slot: str) -> str:
    if slot not in SLOTS:
        raise DiskError(f"unknown ISO slot: {slot} (expected one of {', '.join(SLOTS)})")
    return SLOTS[slot]


def mount_args(vm_spec) -> list[str]:
    args: list[str] = []
    for slot, container_path in SLOTS.items():
        host = vm_spec.media_path(slot)
        if not host:
            continue
        resolved = Path(host).expanduser()
        if not resolved.is_file():
            raise NotFound(
                f"the {slot} ISO for {vm_spec.name} is missing: {resolved}. "
                f"Re-attach it, or clear it with: vmctl iso detach {vm_spec.name} {slot}"
            )
        args += ["-v", f"{resolved.resolve()}:{container_path}:ro"]
    return args
