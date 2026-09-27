from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import DiskError, NotFound

QEMU_IMG = "qemu-img"


def binary() -> str:
    found = shutil.which(QEMU_IMG)
    if not found:
        raise DiskError(f"{QEMU_IMG} not found in PATH")
    return found


def binary_version() -> str:
    proc = subprocess.run([binary(), "--version"], capture_output=True, text=True)
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def run(args: list[str], *, timeout: int | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([binary(), *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DiskError(f"qemu-img {' '.join(args[:4])} failed: {detail}")
    return proc


def info(path: Path, *, force_share: bool = True) -> dict[str, Any]:
    """Raw `qemu-img info` as JSON.

    Format is auto-detected rather than forced with `-f qcow2`: forcing it made
    qemu-img interpret any file as qcow2, so format_of() always answered "qcow2"
    and supports_internal_snapshots() always answered yes, whatever the file
    actually was.
    """
    if not Path(path).is_file():
        raise NotFound(f"no such disk image: {path}")
    args = ["info", "--output=json"]
    if force_share:
        args.append("-U")
    args += ["--", str(path)]
    proc = run(args)
    return json.loads(proc.stdout)


def exists(path: Path) -> bool:
    return Path(path).is_file()


def virtual_size(path: Path) -> int:
    return int(info(path).get("virtual-size", 0))


def actual_size(path: Path) -> int:
    st = os.stat(path)
    return int(st.st_blocks) * 512


def format_of(path: Path, *, data: dict[str, Any] | None = None) -> str:
    return str((data or info(path)).get("format", ""))


def backing_file(path: Path, *, data: dict[str, Any] | None = None) -> str | None:
    return (data or info(path)).get("backing-filename")


def backing_chain(path: Path) -> list[str]:
    proc = run(["info", "--backing-chain", "--output=json", "-f", "qcow2", "--", str(path)])
    chain = json.loads(proc.stdout)
    return [entry.get("filename", "") for entry in chain]


def chain_used(path: Path) -> int:
    """Total allocated bytes across a backing chain.

    A linked clone's own file is tiny; flattening it pulls in the template too,
    so quoting the overlay's size would badly understate the work.
    """
    total = 0
    seen: set[str] = set()
    try:
        files = backing_chain(path)
    except (DiskError, NotFound, OSError):
        files = [str(path)]
    for name in files:
        if name in seen:
            continue
        seen.add(name)
        try:
            total += actual_size(Path(name))
        except OSError:
            continue
    return total or actual_size(path)


def snapshots(
    path: Path, *, data: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    if data is None:
        try:
            data = info(path)
        except DiskError:
            return []
    entries = data.get("snapshots") or []
    result = []
    for entry in entries:
        result.append(
            {
                "name": entry.get("name", ""),
                "id": entry.get("id"),
                "vm-state-size": entry.get("vm-state-size", 0),
                "date-sec": entry.get("date-sec", 0),
                "date-nsec": entry.get("date-nsec", 0),
            }
        )
    result.sort(key=lambda item: (item["date-sec"], item["date-nsec"]))
    return result


def snapshot_names(path: Path, *, data: dict[str, Any] | None = None) -> list[str]:
    return [entry["name"] for entry in snapshots(path, data=data)]


def create(path: Path, size: str | int) -> None:
    args = ["create", "-f", "qcow2", "-o", "preallocation=off", "--", str(path)]
    args.append(str(size) if isinstance(size, str) else str(size))
    run(args)


def create_overlay(path: Path, backing: Path, *, backing_fmt: str = "qcow2") -> None:
    if Path(path).exists():
        raise DiskError(f"refusing to overwrite existing image: {path}")
    if not Path(backing).is_file():
        raise NotFound(f"backing image not found: {backing}")
    run(
        [
            "create",
            "-f",
            "qcow2",
            "-F",
            backing_fmt,
            "-b",
            str(backing),
            "--",
            str(path),
        ]
    )


def apply_snapshot(path: Path, name: str) -> None:
    if name not in snapshot_names(path):
        raise NotFound(f"no such snapshot: {name} in {path}")
    run(["snapshot", "-a", name, "-f", "qcow2", "--", str(path)], timeout=3600)


def delete_snapshot(path: Path, name: str) -> None:
    if name not in snapshot_names(path):
        raise NotFound(f"no such snapshot: {name} in {path}")
    run(["snapshot", "-d", name, "-f", "qcow2", "--", str(path)], timeout=1800)


def convert(src: Path, dst: Path, fmt: str, *, compress: bool = True) -> None:
    if Path(dst).exists():
        raise DiskError(f"destination already exists: {dst}")
    # Let qemu-img probe the source format so this is not qcow2-only.
    args = ["convert", "-p"]
    if fmt != "qcow2" and compress:
        args.append("-c")
    args += ["-O", fmt, "--", str(src), str(dst)]
    run(args, timeout=6 * 3600)


def rebase(path: Path, backing: Path | None, *, backing_fmt: str = "qcow2") -> None:
    if backing is None:
        run(["rebase", "-f", "qcow2", "-u", "-b", "", "--", str(path)])
        return
    run(["rebase", "-f", "qcow2", "-u", "-b", str(backing), "-F", backing_fmt, "--", str(path)])


def check(path: Path, *, repair: bool = False) -> dict[str, Any]:
    # qemu-img only accepts `-r leaks` or `-r all`; without repair there is no
    # -r flag at all.
    args = ["check", "-f", "qcow2"]
    if repair:
        args += ["-r", "all"]
    args += ["--", str(path)]
    proc = run(args, check=False, timeout=3600)
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, 2):
        raise DiskError(f"qemu-img check failed for {path}: {text.strip()}")
    return {
        "returncode": proc.returncode,
        "leaks": "leaks" in text.lower(),
        "corruptions": "corrupt" in text.lower() or "errors" in text.lower(),
        "output": text.strip(),
    }


def supports_internal_snapshots(path: Path) -> bool:
    return format_of(path) == "qcow2"


# qemu-img map walks every allocated extent, so it costs ~160 ms on a 12 GB
# Windows disk against ~3 ms on an empty one. The answer can only change when the
# disk is written, so it is cached against the file's mtime and size.
_data_cache: dict[tuple[str, float, int], bool] = {}
_DATA_CACHE_LIMIT = 256


# Same reasoning as has_data: `info` is a handful of ms per disk, and the
# status poll asks for the same answer every few seconds. Only the polling path
# uses this; correctness-critical callers keep the uncached info().
_info_cache: dict[tuple[str, float, int], dict[str, Any]] = {}


def cached_info(path: Path) -> dict[str, Any]:
    """info() memoised against the image's mtime and size."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _info_cache.get(key)
    if cached is not None:
        return cached
    try:
        data = info(path)
    except (DiskError, NotFound):
        return {}
    if len(_info_cache) > _DATA_CACHE_LIMIT:
        _info_cache.clear()
    _info_cache[key] = data
    return data


def has_data(path: Path) -> bool:
    """True once a guest has written to the image (i.e. it looks installed)."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _data_cache.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [binary(), "map", "-f", "qcow2", "--output=json", "-U", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        entries = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False
    result = any(entry.get("data") for entry in entries)
    if len(_data_cache) > _DATA_CACHE_LIMIT:
        _data_cache.clear()
    _data_cache[key] = result
    return result
