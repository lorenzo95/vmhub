"""cloud-init seeds: render the files and build the CIDATA ISO.

A seed is three small files:

    user-data       what to do (users, keys, packages, runcmd)
    meta-data       instance identity, which controls whether cloud-init runs
    network-config  optional netplan v2 for DHCP

They are put on an ISO whose volume label is CIDATA. cloud-init's NoCloud
datasource finds that label on any attached block device, so the device name
does not matter -- which is why vmhub can use the container's one second
CD-ROM for it.

The rendered files are kept under <vm>/seed/ as well, because the ISO is a
binary blob and the thing a person actually wants to read or edit is the
user-data.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from . import iso, paths
from .errors import ImageError

Progress = Callable[[str], None]

DEFAULT_USER = "debian"
DEFAULT_PACKAGES = ("qemu-guest-agent", "ca-certificates", "curl")
# Linux's own rule, minus the range that would need quoting. Also the
# boundary that keeps `--user` from writing extra YAML into user-data:
# a newline in a name would otherwise become another cloud-config key.
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def read_user_data(path: Path) -> str:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ImageError(f"no such user-data file: {path}")
    text = path.read_text()
    if not text.strip():
        raise ImageError(f"user-data file is empty: {path}")
    return text


def default_user_data(
    *,
    hostname: str,
    user: str = DEFAULT_USER,
    ssh_keys: Iterable[str] = (),
    packages: Iterable[str] = DEFAULT_PACKAGES,
) -> str:
    """The smallest seed that makes SSH work and makes the agent answer.

    Deliberately no desktop, no upgrades and no `runcmd` beyond starting the
    agent. Everything else belongs over SSH, where a failure has an exit code
    and can be retried; a `runcmd` failure is a line in a log nobody reads.
    """
    keys = [key.strip() for key in ssh_keys if key.strip()]
    if not keys:
        raise ImageError(
            "at least one SSH public key is required: pass --ssh-key FILE, "
            "or --seed with your own user-data"
        )
    if not USER_RE.match(user):
        raise ImageError(
            f"invalid guest user name {user!r}: use lowercase letters, digits, "
            f"'_' and '-', starting with a letter or '_'"
        )
    key_lines = "\n".join(f"      - {key}" for key in keys)
    package_lines = "\n".join(f"  - {name}" for name in packages)
    return f"""#cloud-config
hostname: {hostname}
manage_etc_hosts: true
users:
  - name: {user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
{key_lines}
ssh_pwauth: false
# The cloud image ships with empty apt lists, so `packages:` would fail to
# find the agent without this. It costs a few seconds once, on first boot.
package_update: true
packages:
{package_lines}
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
"""


def default_meta_data(*, hostname: str, instance_id: str) -> str:
    return f"""instance-id: {instance_id}
local-hostname: {hostname}
"""


def default_network_config() -> str:
    """DHCP on whatever the virtio NIC ends up being called.

    passt hands out a lease, and the cloud images enable DHCP anyway; this
    file exists so a rename (ens3, enp0s2, eno1) cannot leave the VM with no
    address and therefore no way in.
    """
    return """version: 2
ethernets:
  primary:
    match:
      name: "en*"
    dhcp4: true
"""


def discover_ssh_keys(home: Path | None = None) -> list[str]:
    """Public keys next to the usual private ones, in preference order."""
    base = Path(home or "~").expanduser()
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        candidate = base / ".ssh" / name
        if candidate.is_file():
            text = candidate.read_text().strip()
            if text:
                return [text]
    return []


ISO_TOOLS = ("cloud-localds", "genisoimage", "mkisofs", "xorriso")


def iso_tool() -> str:
    """Path to an ISO builder, or "" if none is installed."""
    for tool in ISO_TOOLS:
        found = shutil.which(tool)
        if found:
            return found
    return ""


def _iso_tool() -> list[str]:
    found = iso_tool()
    if found:
        return [found]
    raise ImageError(
        "no ISO builder found: install cloud-image-utils (preferred) or "
        "genisoimage, e.g. sudo apt-get install cloud-image-utils"
    )


def _build_iso(seed_dir: Path, out: Path, *, user_data: Path, meta_data: Path, network: Path) -> None:
    tool = _iso_tool()
    name = Path(tool[0]).name
    if name == "cloud-localds":
        argv = [
            tool[0],
            f"--network-config={network}",
            str(out),
            str(user_data),
            str(meta_data),
        ]
    elif name == "xorriso":
        argv = [
            tool[0],
            "-as", "mkisofs",
            "-quiet",
            "-output", str(out),
            "-volid", "CIDATA",
            "-joliet",
            "-rock",
            str(user_data), str(meta_data), str(network),
        ]
    else:
        argv = [
            tool[0],
            "-quiet",
            "-output", str(out),
            "-volid", "CIDATA",
            "-joliet",
            "-rock",
            str(user_data), str(meta_data), str(network),
        ]
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=seed_dir)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ImageError(f"building the cloud-init seed ISO failed: {detail}")


def build(
    vm: str,
    *,
    user_data: str,
    instance_id: str = "",
    progress: Progress | None = None,
) -> Path:
    """Write the seed files, build seed.iso, and return its path."""
    say = progress or (lambda _m: None)
    seed_dir = paths.seed_dir(vm)
    seed_dir.mkdir(parents=True, exist_ok=True)

    instance = instance_id.strip() or f"{vm}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    user_path = seed_dir / "user-data"
    meta_path = seed_dir / "meta-data"
    net_path = seed_dir / "network-config"
    out = seed_dir / paths.SEED_ISO

    user_path.write_text(user_data if user_data.endswith("\n") else user_data + "\n")
    meta_path.write_text(default_meta_data(hostname=vm, instance_id=instance))
    net_path.write_text(default_network_config())

    say(f"Building cloud-init seed for {vm} (instance-id {instance})")
    _build_iso(seed_dir, out, user_data=user_path, meta_data=meta_path, network=net_path)

    info = iso.validate(out)
    if info.label.lower() != "cidata":
        raise ImageError(
            f"the seed ISO was built with volume label {info.label!r}, not CIDATA; "
            f"cloud-init will not find it"
        )
    say(f"Seed ready: {out} ({info.human_size}, label {info.label})")
    return out
