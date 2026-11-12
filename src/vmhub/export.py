from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import disk, lifecycle, paths, podman, registry, spec, toml_io
from .errors import AlreadyExists, DiskError, NotFound, VmRunning
from .lifecycle import Progress, _noop

FORMAT = "vmhub-bundle/1"
NVRAM_SUFFIXES = (".rom", ".vars", ".tpm")

# The checksum of a finished bundle lives beside it, in its own file.
#
# The manifest inside the bundle already carries the disk's checksum, but the
# manifest is *inside* the thing that can be truncated, so it cannot report on
# its own integrity. This sidecar is written only after the bundle is complete
# and renamed into place, so its presence means "this archive was finished",
# and its hash is over the compressed bytes -- which is what the filesystem
# actually stores. A bundle with no sidecar is one that was never finished.
SIDECAR_SUFFIX = ".sha256"
# A bundle being written. It never gets the final name, so a half-written
# archive can never be mistaken for a backup.
PARTIAL_SUFFIX = ".partial"

# zstd frame magic. Bundles are sniffed by content, not by extension, so a
# renamed file still opens with the right decompressor.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_binary() -> str | None:
    return shutil.which("zstd")


def _sidecar_path(archive: Path) -> Path:
    return archive.with_name(archive.name + SIDECAR_SUFFIX)


def _read_sidecar(archive: Path) -> str:
    """The checksum recorded for this bundle, or "" when there is none."""
    try:
        text = _sidecar_path(archive).read_text()
    except OSError:
        return ""
    return text.split()[0] if text.split() else ""


def _write_sidecar(archive: Path, digest: str) -> None:
    """Record the bundle's checksum, atomically, beside the bundle itself."""
    sidecar = _sidecar_path(archive)
    tmp = sidecar.with_name(sidecar.name + PARTIAL_SUFFIX)
    try:
        tmp.write_text(f"{digest}  {archive.name}\n")
        os.replace(tmp, sidecar)
    finally:
        tmp.unlink(missing_ok=True)


def _free_bytes(directory: Path) -> int:
    st = os.statvfs(directory)
    return int(st.f_bavail) * int(st.f_frsize)


def _ensure_space(directory: Path, needed: int, what: str) -> None:
    """Refuse a copy that cannot finish, before it starts.

    A full disk does not fail politely. The copy dies part-way and leaves a
    truncated archive behind that looks exactly like a good one -- which is
    the failure this check exists to prevent.
    """
    free = _free_bytes(directory)
    if needed > free:
        raise DiskError(
            f"not enough space to {what}: {spec.format_size(needed)} needed, "
            f"{spec.format_size(free)} free in {directory}"
        )


def _storage_bytes(storage: Path) -> int:
    """How much room the NVRAM and the rest of the storage dir need."""
    total = 0
    try:
        for entry in storage.iterdir():
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return 0
    return total


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
    ext = "tar.zst" if _zstd_binary() else "tar.gz"
    return f"{vm}-{stamp}.vmhub.{ext}"


class _HashingWriter:
    """A file wrapper that hashes every byte written through it.

    The sidecar checksum used to cost a full extra read of the finished
    archive. Hashing the compressed bytes as they land removes that pass
    without changing what the checksum covers.
    """

    def __init__(self, path: Path) -> None:
        self._handle = open(path, "wb")
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self._handle.write(data)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _add_members(tar: tarfile.TarFile, staging: Path) -> None:
    # Add the manifest first, with a bare arcname. read_manifest() on the
    # import side streams the members and short-circuits on the first
    # manifest.json it sees -- so a name-collision check on import can run
    # before the multi-GB disk block has even been decompressed. Without
    # this, every import pays the full cost of decoding past the disk
    # just to find the manifest near the end of the archive.
    manifest = staging / "manifest.json"
    if manifest.is_file():
        tar.add(manifest, arcname="manifest.json")
    tar.add(
        staging, arcname=".",
        # The recursive add below would re-add manifest.json as
        # "./manifest.json"; skip it so it stays a single, bare-named
        # member at the start of the archive.
        filter=lambda ti: None if ti.name == "./manifest.json" else ti,
    )


def _build_tar(staging: Path, dest: Path) -> str:
    """Write the bundle; return the sha256 of the compressed bytes.

    zstd when the binary is present: tens of times faster than gzip at a
    better ratio, and -T0 uses every core. The payload is still a plain
    tar inside a standard zstd frame. Without zstd the fallback is gzip
    at level 1 -- the old level 9 spent ~9x the CPU for ~3% smaller
    archives, which is a bad trade for a backup.
    """
    zstd = _zstd_binary()
    out = _HashingWriter(dest)
    try:
        if zstd:
            proc = subprocess.Popen(
                [zstd, "-q", "-3", "-T0", "-c"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            # Drain zstd's output while tar is still writing its input.
            # Waiting until the tar is finished deadlocks on any real
            # disk: the compressed output fills the 64 KB pipe buffer,
            # zstd blocks on stdout, and tar blocks on stdin.
            errors: list[BaseException] = []

            def _drain() -> None:
                try:
                    while True:
                        block = proc.stdout.read(1 << 22)
                        if not block:
                            break
                        out.write(block)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    proc.kill()  # unblock the tar writer

            drainer = threading.Thread(target=_drain, name="vmhub-bundle-hash", daemon=True)
            drainer.start()
            tar_error: BaseException | None = None
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                    _add_members(tar, staging)
                proc.stdin.close()
            except BaseException as exc:  # noqa: BLE001
                tar_error = exc
                proc.kill()
            drainer.join()
            proc.stdout.close()
            proc.wait()
            if errors:
                raise errors[0]
            if tar_error is not None:
                raise tar_error
            if proc.returncode != 0:
                raise DiskError("zstd failed while writing the bundle")
        else:
            with tarfile.open(fileobj=out, mode="w|gz", compresslevel=1) as tar:
                _add_members(tar, staging)
    finally:
        out.close()
    return out.digest.hexdigest()


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
        exported = staging / "disk" / paths.DISK_NAME

        # The copy below needs room for the disk as it is allocated on disk,
        # plus the NVRAM beside it. Checked first, so a disk that is nearly
        # full says so now instead of leaving a truncated bundle behind.
        _ensure_space(
            target,
            disk.actual_size(disk_file) + _storage_bytes(paths.storage_dir(vm)),
            f"export {vm}",
        )

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
            disk.convert(disk_file, exported, "qcow2")
            disk_sha = _sha256(exported)
        else:
            # Standalone disk: copy it. Convert was used here too, and it
            # dropped the disk's internal snapshots -- the bundle came back
            # with nothing to revert to. This used to say "Copying disk
            # (sparse-aware)" while converting anyway.
            progress("Copying disk (sparse-aware)...")
            # The copy is byte-for-byte, so hash the source now instead of
            # re-reading the staged copy afterwards: one full pass saved.
            disk_sha = _sha256(disk_file)
            disk.copy_sparse(disk_file, exported)
        disk_entry = {
            "file": paths.DISK_NAME,
            "sha256": disk_sha,
            "virtual_size": disk.virtual_size(disk_file),
            "actual_size": disk.actual_size(exported),
            "format": disk.format_of(exported),
            # Read off the exported disk, which is now the source itself for a
            # standalone VM -- so this lists what the bundle actually carries
            # rather than what a convert happened to drop.
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
            # The port list travels without its host numbers, which belong
            # to the machine the bundle was made on. The entries themselves
            # do travel, so a user's own forwards survive the trip with
            # their names; the import gives each one a port of its own.
            network=spec.Network(
                mode=vm_spec.network.mode,
                guest_ports=list(vm_spec.network.guest_ports),
                ports=[spec.Port(guest=p.guest, host=0, role=p.role, name=p.name)
                       for p in vm_spec.network.ports],
                bind=vm_spec.network.bind,
                mac="",
                ip="",
            ),
            # Never carry the console password out of the bundle. The
            # rest of Display is cosmetic and worth keeping; the password is
            # a secret, and protected must be cleared with it because
            # spec.validate() rejects protected without a password.
            display=spec.Display(
                protected=False,
                password="",
                audio=vm_spec.display.audio,
                gpu=vm_spec.display.gpu,
                lossy=vm_spec.display.lossy,
                vga=vm_spec.display.vga,
            ),
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
            "image": vm_spec.image.ref,
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
        progress(
            "Writing archive (zstd, all cores)..."
            if _zstd_binary()
            else "Writing archive (gzip fallback; install zstd for much faster backups)..."
        )
        # Written to a partial name and renamed into place only once it is
        # whole. A bundle that is cut short -- by a full disk, by a killed
        # job, by anything -- therefore never appears under the name a backup
        # is looked up by. Before this, the archive was written straight to
        # its final path, so a failure left a truncated file that `vmctl
        # bundles` listed like any other backup.
        partial = target / (archive.name + PARTIAL_SUFFIX)
        try:
            digest = _build_tar(staging, partial)
            os.replace(partial, archive)
        finally:
            partial.unlink(missing_ok=True)
        # Written after the bundle is in place, so a sidecar exists only for
        # a bundle that was finished.
        _write_sidecar(archive, digest)
    finally:
        shutil.rmtree(tmpname, ignore_errors=True)

    progress(f"Exported {vm} -> {archive} ({spec.format_size(archive.stat().st_size)})")
    return archive


@contextmanager
def _open_tar(archive: Path):
    """Open a bundle for reading, whatever it is compressed with.

    zstd is sniffed by magic and piped through the zstd binary: Python
    3.13's tarfile cannot read zstd itself. Everything else goes through
    tarfile's auto-detection, which keeps every older gzip bundle
    readable. The stream is one-pass, so callers must iterate members in
    order (both read_manifest and import_bundle do).
    """
    with open(archive, "rb") as probe:
        magic = probe.read(4)
    if magic == ZSTD_MAGIC:
        zstd = _zstd_binary()
        if not zstd:
            raise DiskError(
                f"{Path(archive).name} is zstd-compressed; install zstd to read it"
            )
        proc = subprocess.Popen(
            [zstd, "-d", "-c", "--", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                yield tar
            proc.stdout.close()
            # The consumer may have stopped early -- read_manifest only
            # needs the first member. A zstd that is still running would
            # then die on the closed pipe, which is not a bundle error;
            # only a zstd that finished on its own can report one.
            rc = proc.poll()
            if rc is None:
                proc.kill()
                proc.wait()
            else:
                proc.wait()
                if rc != 0:
                    detail = proc.stderr.read().decode(errors="replace").strip()
                    raise DiskError(f"decompressing {Path(archive).name} failed: {detail}")
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    else:
        with tarfile.open(archive, "r:*") as tar:
            yield tar


def read_manifest(archive: Path) -> dict[str, Any]:
    # Stream the gzip archive header-by-header via tar.next() instead of
    # getmembers(), which decodes the entire archive up front. Bundles built
    # after _build_tar's manifest-first ordering lands put manifest.json in the
    # first member; tar.next() returns it before the disk member is reached,
    # so an import that hits AlreadyExists never has to decompress the disk.
    # Older bundles (manifest near the end) still work -- the loop walks the
    # whole archive, but the import flow only pays that cost when the name is
    # actually free.
    try:
        with _open_tar(archive) as tar:
            while True:
                member = tar.next()
                if member is None:
                    break
                if member.name.endswith("manifest.json"):
                    handle = tar.extractfile(member)
                    if handle is None:
                        break
                    try:
                        return json.loads(handle.read().decode())
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise DiskError(
                            f"manifest in {Path(archive).name} is damaged: {exc}"
                        ) from exc
    except tarfile.TarError as exc:
        # A damaged archive must fail as a vmhub error the CLI can report,
        # not as a raw tarfile traceback.
        raise DiskError(f"cannot read {Path(archive).name}: {exc}") from exc
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
        raise AlreadyExists(
            f"VM already exists: {target_name} (choose another name)",
            name=target_name,
        )

    progress(f"Importing {target_name} from {Path(archive).name}...")
    # Stage beside the backups directory, not in /tmp: a bundle can be tens of
    # GB and /tmp is frequently a small tmpfs (and a 4 GB compressed qcow2
    # routinely decompresses past it). Mirrors export_vm's policy above.
    tmpname = tempfile.mkdtemp(prefix=f"vmhub-import-{target_name}-", dir=paths.backup_dir())
    try:
        tmp = Path(tmpname)
        disk_name = manifest.get("disk", {}).get("file", paths.DISK_NAME)
        disk_digest: str | None = None
        with _open_tar(archive) as tar:
            # One pass: check each member's path and extract it in the same
            # step. A streaming zstd pipe cannot be rewound for a second
            # scan, and the disk is hashed while it is extracted so the
            # verification below costs no extra read.
            for member in tar:
                target = (tmp / member.name).resolve()
                if not str(target).startswith(str(tmp.resolve())):
                    raise DiskError(f"unsafe path in archive: {member.name}")
                parts = PurePosixPath(member.name).parts
                is_disk = (
                    member.isfile()
                    and len(parts) >= 2
                    and parts[-1] == disk_name
                    and parts[-2] in ("disk", "storage")
                )
                if is_disk:
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise DiskError(f"cannot read {member.name} from the bundle")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with open(target, "wb") as out:
                        while True:
                            block = handle.read(1 << 22)
                            if not block:
                                break
                            digest.update(block)
                            out.write(block)
                    disk_digest = digest.hexdigest()
                else:
                    tar.extract(member, tmp, filter="data")
        bundle_root = tmp / "bundle" if (tmp / "bundle" / "manifest.json").is_file() else tmp
        if not (bundle_root / "manifest.json").is_file():
            raise DiskError("archive does not contain a bundle root")

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
            actual = disk_digest if disk_digest is not None else _sha256(src_disk)
            if actual != expected:
                raise DiskError(
                    f"checksum mismatch for {disk_name}: expected {expected[:16]}…, got {actual[:16]}…"
                )

        paths.disk_dir(target_name).mkdir(parents=True, exist_ok=True)
        paths.storage_dir(target_name).mkdir(parents=True, exist_ok=True)
        # Move the staged disk into place rather than copying it: the bundle
        # already lives in backups/ under the project root, which is the same
        # filesystem as the VM directory, so rename is atomic and avoids a
        # full second write of a multi-GB image. Unlink first because rename
        # silently overwrites on POSIX but only when the source and
        # destination are the same file -- a stale qcow2 left behind by a
        # previous failed attempt would be replaced otherwise, which is
        # exactly the failure mode this path exists to avoid.
        disk_dest = paths.disk_path(target_name)
        disk_dest.unlink(missing_ok=True)
        os.rename(src_disk, disk_dest)
        for entry in (bundle_root / "storage").iterdir():
            if entry.is_file():
                shutil.copy2(entry, paths.storage_dir(target_name) / entry.name)

        imported_spec = spec.spec_from_dict(manifest.get("spec") or {})
        imported_spec.name = target_name
        from . import ports as ports_mod

        wanted = imported_spec.network.ports or ports_mod.defaults(
            imported_spec.network.guest_ports
        )
        imported_spec.network.ports = ports_mod.reallocate(wanted)
        imported_spec.validate()
        registry.save_spec(imported_spec)

        old_meta = manifest.get("meta") or {}
        registry.create_meta(
            target_name,
            blueprint=imported_spec.blueprint,
            is_template=bool(manifest.get("was_template")),
            notes=f"imported from {Path(archive).name}",
            **({"uuid": old_meta.get("uuid", spec.new_uuid()),
                "mac": old_meta.get("mac", spec.new_mac())} if keep_identity else {}),
        )
    finally:
        shutil.rmtree(tmpname, ignore_errors=True)

    if keep_identity:
        progress(f"Imported {target_name} keeping original UUID/MAC")
    else:
        progress(f"Imported {target_name} with fresh UUID/MAC")
    return target_name


def backup(vm: str, *, dest_dir: Path | None = None, progress: Progress = _noop) -> Path:
    return export_vm(vm, dest_dir=dest_dir, progress=progress)


def list_bundles(
    dest_dir: Path | None = None, *, verify: bool = False
) -> list[dict[str, Any]]:
    target = Path(dest_dir) if dest_dir else paths.backup_dir()
    if not target.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for archive in sorted(target.glob("*.vmhub.tar.*")):
        # The sidecar and the half-written file live beside the bundles and
        # match the same glob. Neither is a bundle, and listing them as one
        # would have the health check reporting a backup that is not there.
        if archive.name.endswith((SIDECAR_SUFFIX, PARTIAL_SUFFIX)):
            continue
        entry: dict[str, Any] = {"path": str(archive), "size": archive.stat().st_size}
        expected = _read_sidecar(archive)
        entry["checksum"] = expected
        if expected:
            if verify:
                actual = _sha256(archive)
                entry["verified"] = actual == expected
                if actual != expected:
                    entry["error"] = (
                        "checksum mismatch: the bundle is damaged "
                        f"(expected {expected[:16]}…, got {actual[:16]}…)"
                    )
            else:
                # Not hashed yet. The sidecar is there, so the bundle was
                # finished; only the comparison is outstanding.
                entry["verified"] = None
        else:
            # No sidecar. A bundle only ever gets its final name once it is
            # whole, so this is a bundle made before the checksum existed --
            # unverifiable, not damaged.
            entry["verified"] = False
            entry["note"] = "no checksum recorded: this bundle predates checksums"
        try:
            manifest = read_manifest(archive)
        except Exception as exc:
            entry.setdefault("error", str(exc))
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
