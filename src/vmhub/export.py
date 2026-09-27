from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import disk, lifecycle, paths, podman, registry, spec, toml_io
from .errors import AlreadyExists, DiskError, NotFound, VmRunning
from .lifecycle import Progress, _noop

FORMAT = "vmhub-bundle/1"
NVRAM_SUFFIXES = (".rom", ".vars", ".tpm")


def _sha256(path: Path, *, chunk: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _archive_name(vm: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{vm}-{stamp}.vmhub.tar.gz"


def _build_tar(staging: Path, dest: Path) -> Path:
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(staging, arcname=".")
    return dest


def export_vm(
    vm: str,
    *,
    dest_dir: Path | None = None,
    include_nvram: bool = True,
    progress: Progress = _noop,
) -> Path:
    if not registry.exists(vm):
        raise NotFound(f"no such VM: {vm}")
    if podman.is_running(vm):
        raise VmRunning(f"stop {vm} before exporting it")

    meta = registry.load_meta(vm)
    vm_spec = registry.load_spec(vm)
    disk_file = paths.disk_path(vm)
    if not disk.exists(disk_file):
        raise NotFound(f"{vm} has no disk to export")

    target = Path(dest_dir) if dest_dir else paths.backup_dir()
    target.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination, not in /tmp: a bundle can be tens of GB and
    # /tmp is frequently a small tmpfs.
    tmpname = tempfile.mkdtemp(prefix=".vmhub-export-", dir=target)
    progress(f"Exporting {vm}...")
    try:
        staging = Path(tmpname) / "bundle"
        (staging / "storage").mkdir(parents=True)
        (staging / "disk").mkdir(parents=True)

        chain = disk.backing_chain(disk_file)
        if len(chain) > 1:
            # qemu-img convert resolves the chain as it writes, so the exported
            # disk is standalone. Copying the overlay file instead would carry a
            # backing reference to a path that is not in the bundle, producing an
            # archive that cannot be imported anywhere.
            progress(
                f"Resolving a {len(chain)}-image backing chain into a standalone disk. "
                f"The chain holds {spec.format_size(disk.chain_used(disk_file))} of real "
                f"data, so this copies far more than the clone alone:"
            )
        else:
            progress("Copying disk (sparse-aware)...")
        disk.convert(disk_file, staging / "disk" / paths.DISK_NAME, "qcow2")
        exported = staging / "disk" / paths.DISK_NAME
        disk_entry = {
            "file": paths.DISK_NAME,
            "sha256": _sha256(exported),
            "virtual_size": disk.virtual_size(disk_file),
            "actual_size": disk.actual_size(exported),
            "format": disk.format_of(exported),
            "snapshots": disk.snapshot_names(exported),
            "backing": None,
        }

        if include_nvram:
            for suffix in NVRAM_SUFFIXES:
                src = paths.storage_dir(vm) / f"{paths.NVRAM_STEM}{suffix}"
                if src.is_file():
                    shutil.copy2(src, staging / "storage" / src.name)

        portable = spec.VmSpec(
            name=vm,
            blueprint=vm_spec.blueprint,
            image=vm_spec.image,
            boot=spec.Boot(mode=vm_spec.boot.mode, iso="", media_type=vm_spec.boot.media_type),
            resources=vm_spec.resources,
            network=spec.Network(
                mode=vm_spec.network.mode,
                guest_ports=list(vm_spec.network.guest_ports),
                bind=vm_spec.network.bind,
                mac="",
                ip="",
            ),
            display=vm_spec.display,
            features=vm_spec.features,
        )
        (staging / paths.SPEC_FILE).write_text(
            toml_io.dumps(portable.to_dict(), header=f"vmhub spec for {vm} (exported, portable)")
        )

        manifest = {
            "format": FORMAT,
            "name": vm,
            "created": time.time(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "image": meta.image or vm_spec.image.ref,
            "qemu_img": disk.binary_version(),
            "podman": podman.version(),
            "was_template": meta.is_template,
            "uuid": meta.uuid,
            "mac": meta.mac,
            "spec": portable.to_dict(),
            "meta": meta.to_dict(),
            "disk": disk_entry,
            "files": sorted(p.name for p in (staging / "disk").iterdir()),
            "storage_files": sorted(p.name for p in (staging / "storage").iterdir()),
            "resolved_backing_chain": len(chain) > 1,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        archive = target / _archive_name(vm)
        progress("Writing archive...")
        _build_tar(staging, archive)
    finally:
        shutil.rmtree(tmpname, ignore_errors=True)

    progress(f"Exported {vm} -> {archive} ({spec.format_size(archive.stat().st_size)})")
    return archive


def read_manifest(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            if member.name.endswith("manifest.json"):
                handle = tar.extractfile(member)
                if handle is None:
                    break
                return json.loads(handle.read().decode())
    raise NotFound(f"no manifest.json inside {archive}")


def import_bundle(
    archive: Path,
    *,
    name: str | None = None,
    keep_identity: bool = False,
    progress: Progress = _noop,
) -> str:
    if not Path(archive).is_file():
        raise NotFound(f"no such bundle: {archive}")
    manifest = read_manifest(Path(archive))
    if not str(manifest.get("format", "")).startswith("vmhub-bundle/"):
        raise DiskError(f"{archive} is not a vmhub bundle")

    target_name = spec.validate_name(name or manifest.get("name", "imported"))
    if registry.exists(target_name):
        raise AlreadyExists(f"VM already exists: {target_name} (choose another name)")

    progress(f"Importing {target_name} from {Path(archive).name}...")
    with tempfile.TemporaryDirectory(prefix=f"vmhub-import-{target_name}-") as tmpname:
        tmp = Path(tmpname)
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                target = (tmp / member.name).resolve()
                if not str(target).startswith(str(tmp.resolve())):
                    raise DiskError(f"unsafe path in archive: {member.name}")
            tar.extractall(tmp, filter="data")
        bundle_root = tmp / "bundle" if (tmp / "bundle" / "manifest.json").is_file() else tmp
        if not (bundle_root / "manifest.json").is_file():
            raise DiskError("archive does not contain a bundle root")

        disk_name = manifest.get("disk", {}).get("file", paths.DISK_NAME)
        src_disk = bundle_root / "disk" / disk_name
        if not src_disk.is_file():
            legacy = bundle_root / "storage" / disk_name
            if legacy.is_file():
                src_disk = legacy
            else:
                raise DiskError(f"bundle is missing its disk ({disk_name})")

        expected = manifest.get("disk", {}).get("sha256")
        if expected:
            progress("Verifying checksum...")
            actual = _sha256(src_disk)
            if actual != expected:
                raise DiskError(
                    f"checksum mismatch for {disk_name}: expected {expected[:16]}…, got {actual[:16]}…"
                )

        paths.disk_dir(target_name).mkdir(parents=True, exist_ok=True)
        paths.storage_dir(target_name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_disk, paths.disk_path(target_name))
        for entry in (bundle_root / "storage").iterdir():
            if entry.is_file():
                shutil.copy2(entry, paths.storage_dir(target_name) / entry.name)

        imported_spec = spec.spec_from_dict(manifest.get("spec") or {})
        imported_spec.name = target_name
        from . import ports as ports_mod

        allocated = ports_mod.allocate_all()
        imported_spec.network.host_web = allocated["web"]
        imported_spec.network.host_ssh = allocated["ssh"]
        if 3389 in imported_spec.network.guest_ports:
            imported_spec.network.host_rdp = allocated["rdp"]
        imported_spec.validate()
        registry.save_spec(imported_spec)

        old_meta = manifest.get("meta") or {}
        registry.create_meta(
            target_name,
            blueprint=imported_spec.blueprint,
            image=manifest.get("image") or imported_spec.image.ref,
            is_template=bool(manifest.get("was_template")),
            notes=f"imported from {Path(archive).name}",
            **({"uuid": old_meta.get("uuid", spec.new_uuid()),
                "mac": old_meta.get("mac", spec.new_mac())} if keep_identity else {}),
        )

    if keep_identity:
        progress(f"Imported {target_name} keeping original UUID/MAC")
    else:
        progress(f"Imported {target_name} with fresh UUID/MAC")
    return target_name


def backup(vm: str, *, dest_dir: Path | None = None, progress: Progress = _noop) -> Path:
    return export_vm(vm, dest_dir=dest_dir, progress=progress)


def list_bundles(dest_dir: Path | None = None) -> list[dict[str, Any]]:
    target = Path(dest_dir) if dest_dir else paths.backup_dir()
    if not target.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for archive in sorted(target.glob("*.vmhub.tar.*")):
        entry: dict[str, Any] = {"path": str(archive), "size": archive.stat().st_size}
        try:
            manifest = read_manifest(archive)
        except Exception as exc:
            entry["error"] = str(exc)
        else:
            entry.update(
                {
                    "name": manifest.get("name"),
                    "created_iso": manifest.get("created_iso"),
                    "image": manifest.get("image"),
                    "snapshots": manifest.get("disk", {}).get("snapshots", []),
                    "virtual_size": manifest.get("disk", {}).get("virtual_size", 0),
                }
            )
        out.append(entry)
    return out
