"""Cloud images: fetch, verify, and install into a VM's own disk.

The image is *converted into* ``disk/data.qcow2``. Handing it to the
container as ``BOOT`` media instead would attach it as a separate boot
disk, and the guest would then live outside the disk that snapshots,
clones, resize and export all operate on. That mistake is silent: the VM
boots, but `vmctl snapshot` is snapshotting an empty drive.

Downloads are cached outside the checkout (see paths.image_cache_dir) and
verified against the publisher's SHA512SUMS. A cached file is trusted only
after its digest is checked again, so a truncated download from last month
cannot become a VM today.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import disk, paths, registry, spec
from .errors import ImageError, NotFound

Progress = Callable[[str], None]
CHUNK = 1024 * 1024
USER_AGENT = "vmhub/1 (+https://github.com/lorenzo95/vmhub)"

DEBIAN_BASE = "https://cloud.debian.org/images/cloud/trixie/latest"


@dataclass(frozen=True)
class CloudImage:
    key: str
    filename: str
    base: str
    title: str

    @property
    def url(self) -> str:
        return f"{self.base}/{self.filename}"

    @property
    def sums_url(self) -> str:
        return f"{self.base}/SHA512SUMS"


# `genericcloud` is the variant that carries cloud-init and openssh-server.
# Debian's `nocloud` image is misleadingly named for this purpose: it has
# neither, so a CIDATA seed is ignored and the guest drops into an
# interactive systemd-firstboot prompt on the serial console. Offering it
# here would produce a VM with no user, no SSH and no agent -- every check
# would look like a slow boot until the wait gave up.
_CATALOG = (
    CloudImage(
        "debian13",
        "debian-13-genericcloud-amd64.qcow2",
        DEBIAN_BASE,
        "Debian 13 (genericcloud)",
    ),
)
ALIASES = {
    "debian": "debian13",
    "trixie": "debian13",
    "debian13-genericcloud": "debian13",
}
CATALOG = {image.key: image for image in _CATALOG}


def resolve(key: str) -> CloudImage:
    name = (key or "").strip()
    if name in ALIASES:
        name = ALIASES[name]
    image = CATALOG.get(name)
    if image is None:
        choices = ", ".join(sorted(CATALOG))
        raise NotFound(f"unknown cloud image {key!r}; choose one of: {choices}")
    return image


def cached_path(image: CloudImage) -> Path:
    return paths.image_cache_dir() / image.filename


def sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """Filename -> digest from a SHA512SUMS file.

    The format is ``<128 hex>  <name>`` with two spaces, optionally a
    ``*`` before the name for binary mode. Only the fields we need are
    read; a malformed line is skipped rather than aborting the whole file,
    because one odd comment should not make every image unfetchable.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        digest, name = parts
        if len(digest) != 128 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            continue
        out[name.lstrip("*")] = digest.lower()
    return out


def fetch(
    key: str,
    *,
    expect_sha512: str = "",
    progress: Progress | None = None,
    timeout: float = 60.0,
) -> tuple[Path, str]:
    """Return (cached image, its sha512), downloading it if needed.

    A cached file is not trusted just because it exists: its digest is
    compared with the publisher's SHA512SUMS every time. Checking a file
    against itself would happily accept last month's truncated download.
    Pin `expect_sha512` to skip that network round trip.
    """
    say = progress or (lambda _m: None)
    image = resolve(key)
    dest = cached_path(image)
    expected = expect_sha512.lower()

    if not expected:
        say(f"Fetching {image.sums_url}")
        try:
            expected = _download_sums(image)
        except (urllib.error.URLError, OSError) as exc:
            raise ImageError(
                f"cannot read {image.sums_url} to verify {image.filename}: {exc}. "
                f"Pass --sha512 to pin the digest yourself, or retry when online."
            ) from exc

    if dest.is_file() and dest.stat().st_size > 0:
        digest = sha512_file(dest)
        if digest == expected:
            say(f"Using cached {image.filename} ({dest})")
            return dest, digest
        say(f"Cached {image.filename} does not match {image.sums_url}; downloading again")
        dest.unlink()

    say(f"Downloading {image.title} ({image.filename})")
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha512()
    try:
        request = urllib.request.Request(image.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            seen = 0
            next_report = 10
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    seen += len(chunk)
                    if total:
                        percent = seen * 100 // total
                        if percent >= next_report:
                            say(f"  {percent}% of {total // (1024 * 1024)} MB")
                            next_report = percent + 10
        actual = digest.hexdigest()
        if actual != expected:
            raise ImageError(
                f"{image.filename} failed verification: expected {expected}, got {actual}. "
                f"The download was removed; nothing was installed."
            )
        os.replace(part, dest)
    except (urllib.error.URLError, OSError) as exc:
        raise ImageError(f"downloading {image.url} failed: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)

    say(f"Verified {image.filename} sha512 {actual[:16]}...")
    return dest, actual


def _download_sums(image: CloudImage) -> str:
    request = urllib.request.Request(image.sums_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", "replace")
    sums = parse_sums(text)
    digest = sums.get(image.filename)
    if not digest:
        raise ImageError(f"{image.sums_url} does not list {image.filename}")
    return digest


def install(
    vm: str,
    source: Path,
    *,
    progress: Progress | None = None,
    key: str = "",
    digest: str = "",
) -> dict:
    """Convert `source` into the VM's own disk and grow it to the spec size."""
    say = progress or (lambda _m: None)
    vm_spec = registry.load_spec(vm)
    dest = paths.disk_path(vm)
    if not paths.disk_dir(vm).is_dir():
        raise NotFound(f"{vm}: no disk directory (is it a vmhub VM?)")

    incoming = dest.with_name(dest.name + ".incoming")
    incoming.unlink(missing_ok=True)
    say(f"Converting {Path(source).name} into {dest}")
    try:
        disk.run(
            ["convert", "-O", "qcow2", "-o", "preallocation=off", str(source), str(incoming)],
            timeout=None,
        )
        disk.run(["resize", str(incoming), str(vm_spec.resources.disk_bytes)], timeout=300)
        # Replace the placeholder disk only after both steps succeeded. A
        # failed convert must leave the VM with the disk it had, not with a
        # half-written image that `start` would happily boot.
        os.replace(incoming, dest)
    finally:
        incoming.unlink(missing_ok=True)

    record = {
        "image": Path(source).name,
        "key": key,
        "source": str(Path(source).resolve()),
        "url": resolve(key).url if key else "",
        "sha512": digest or (sha512_file(Path(source)) if Path(source).is_file() else ""),
        "installed": time.time(),
        "disk": str(dest),
        "virtual_size": spec.format_size(vm_spec.resources.disk_bytes),
    }
    _write_provenance(vm, record)
    say(f"Installed {Path(source).name} as {vm}'s disk")
    return record


def install_key(vm: str, key: str, *, progress: Progress | None = None) -> dict:
    """fetch() then install(), recording the URL and digest that were used."""
    source, digest = fetch(key, progress=progress)
    record = install(vm, source, progress=progress)
    image = resolve(key)
    record["image"] = image.filename
    record["url"] = image.url
    record["sha512"] = digest
    _write_provenance(vm, record)
    return record


def _write_provenance(vm: str, record: dict) -> None:
    path = paths.vm_dir(vm) / "image.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def inventory() -> list[dict]:
    """The catalog plus what is cached, for `vmctl images`."""
    rows = []
    for key in sorted(CATALOG):
        image = CATALOG[key]
        path = cached_path(image)
        row = {
            "key": key,
            "title": image.title,
            "filename": image.filename,
            "url": image.url,
            "cached": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
            "path": str(path) if path.is_file() else "",
        }
        rows.append(row)
    return rows
