from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import iso, paths, spec
from .errors import NotFound, PodmanError

CONTAINER_PREFIX = "vmhub-"
LABEL_MANAGED = "vmhub.managed"
LABEL_NAME = "vmhub.name"
LABEL_UUID = "vmhub.uuid"
LABEL_SPEC = "vmhub.spec"
STOP_TIMEOUT = 120


def binary() -> str:
    found = shutil.which("podman")
    if not found:
        raise PodmanError("podman not found in PATH")
    return found


def container_name(vm: str) -> str:
    return f"{CONTAINER_PREFIX}{vm}"


def base_argv() -> list[str]:
    return [binary()]


def run(args: Sequence[str], *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    argv = base_argv() + list(args)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PodmanError(f"podman {' '.join(args[:3])} failed ({proc.returncode}): {detail}")
    return proc


def version() -> str:
    return run(["version", "--format", "{{.Client.Version}}"]).stdout.strip()


def pull(image_ref: str) -> None:
    run(["pull", image_ref], timeout=1800)


def image_present(image_ref: str) -> bool:
    proc = run(["image", "exists", image_ref], check=False)
    return proc.returncode == 0


def exists(vm: str) -> bool:
    return run(["container", "exists", container_name(vm)], check=False).returncode == 0


@dataclass
class Container:
    """One row of `podman ps`, enough to answer everything status() asks."""

    name: str
    vm: str
    state: str
    exit_code: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def managed(self) -> bool:
        return self.labels.get(LABEL_MANAGED) == "true"

    @property
    def powered(self) -> bool:
        return self.state in {"running", "paused"}


def ps() -> dict[str, Container]:
    """Every container in a single call, keyed by VM name.

    status() used to spend two or three podman invocations per VM (exists,
    inspect, inspect again); one `podman ps --format json` answers all of it in
    about 19 ms and is the difference between a responsive UI and a frozen one.
    """
    proc = run(["ps", "-a", "--format", "json"], check=False)
    out: dict[str, Container] = {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return out
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return out
    for row in rows:
        names = row.get("Names") or []
        container = names[0] if isinstance(names, list) and names else str(names or "")
        if not container.startswith(CONTAINER_PREFIX):
            continue
        labels = row.get("Labels") or {}
        vm = labels.get(LABEL_NAME) or container[len(CONTAINER_PREFIX) :]
        out[vm] = Container(
            name=container,
            vm=vm,
            state=str(row.get("State") or "unknown").lower(),
            exit_code=int(row.get("ExitCode") or 0),
            labels=labels,
        )
    return out


def inspect(vm: str) -> dict[str, Any]:
    proc = run(["inspect", container_name(vm)])
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return data[0] if data else {}
    return data


def state(vm: str) -> str:
    if not exists(vm):
        return "absent"
    data = inspect(vm)
    status = (data.get("State") or {}).get("Status")
    if not status:
        return "unknown"
    if status == "running":
        paused = (data.get("State") or {}).get("Paused")
        return "paused" if paused else "running"
    return status


def is_running(vm: str) -> bool:
    return state(vm) in {"running", "paused"}


def managed(vm: str) -> bool:
    if not exists(vm):
        return False
    labels = ((inspect(vm).get("Config") or {}).get("Labels")) or {}
    return labels.get(LABEL_MANAGED) == "true"


def _volume_args(
    vm: str,
    template_disk: Path | None,
    host_share: str = "",
    vm_spec: "spec.VmSpec | None" = None,
) -> list[str]:
    disk = paths.disk_path(vm).resolve()
    args = [
        "-v",
        f"{disk}:/data.qcow2",
        "-v",
        f"{disk}:{paths.DISK_SHADOW}",
        "-v",
        f"{paths.storage_dir(vm).resolve()}:/storage",
    ]
    if vm_spec is not None:
        args += iso.mount_args(vm_spec)
    if template_disk is not None:
        resolved = template_disk.resolve()
        args += ["-v", f"{resolved}:{resolved}:ro"]
    if host_share:
        args += ["-v", f"{host_share}:/shared"]
    return args


def spec_hash(
    vm_spec: spec.VmSpec, meta_uuid: str, meta_mac: str, template_disk: Path | None
) -> str:
    import hashlib

    payload = {
        "env": vm_spec.env(uuid=meta_uuid, mac=meta_mac),
        "image": vm_spec.image.ref,
        "ports": {str(k): v for k, v in sorted(vm_spec.network.published().items())},
        "bind": vm_spec.network.bind,
        "template": str(template_disk) if template_disk else "",
        "host_share": vm_spec.features.host_share,
        "install_iso": vm_spec.media.install,
        "drivers_iso": vm_spec.media.drivers,
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:32]


def run_argv(vm_spec: spec.VmSpec, meta_uuid: str, meta_mac: str, template_disk: Path | None) -> list[str]:
    args = [
        "run",
        "-d",
        "--name",
        container_name(vm_spec.name),
        "--label",
        f"{LABEL_MANAGED}=true",
        "--label",
        f"{LABEL_NAME}={vm_spec.name}",
        "--label",
        f"{LABEL_UUID}={meta_uuid}",
        "--label",
        f"{LABEL_SPEC}={spec_hash(vm_spec, meta_uuid, meta_mac, template_disk)}",
        "--device",
        "/dev/kvm",
        "--device",
        "/dev/net/tun",
        "--cap-add",
        "NET_ADMIN",
        "--group-add",
        "keep-groups",
    ]
    bind = vm_spec.network.bind or "127.0.0.1"
    for host_port, guest_port in sorted(vm_spec.network.published().items()):
        args += ["-p", f"{bind}:{host_port}:{guest_port}"]
    args += _volume_args(vm_spec.name, template_disk, _share_path(vm_spec), vm_spec)
    for key, value in sorted(vm_spec.env(uuid=meta_uuid, mac=meta_mac).items()):
        args += ["-e", f"{key}={value}"]
    args.append(vm_spec.image.ref)
    return args


def _share_path(vm_spec: spec.VmSpec) -> str:
    share = vm_spec.features.host_share
    if not share:
        return ""
    path = Path(share)
    if not path.is_absolute():
        path = paths.project_root() / share
    return str(path.resolve())


def create(
    vm_spec: spec.VmSpec,
    meta_uuid: str,
    meta_mac: str,
    template_disk: Path | None = None,
) -> str:
    if exists(vm_spec.name):
        raise PodmanError(f"container already exists: {container_name(vm_spec.name)}")
    paths.disk_dir(vm_spec.name).mkdir(parents=True, exist_ok=True)
    paths.storage_dir(vm_spec.name).mkdir(parents=True, exist_ok=True)
    proc = run(run_argv(vm_spec, meta_uuid, meta_mac, template_disk), timeout=300)
    return proc.stdout.strip()


def start(vm: str) -> None:
    run(["start", container_name(vm)])


def stop(vm: str, *, timeout: int = STOP_TIMEOUT) -> None:
    if not exists(vm):
        raise NotFound(f"no container for VM: {vm}")
    run(["stop", "--time", str(timeout), container_name(vm)], timeout=timeout + 60)


def kill(vm: str) -> None:
    run(["kill", container_name(vm)])


def rm(vm: str, *, force: bool = True) -> None:
    if not exists(vm):
        return
    args = ["rm"]
    if force:
        args.append("-f")
    args.append(container_name(vm))
    run(args, timeout=180)


def pause(vm: str) -> None:
    run(["pause", container_name(vm)])


def unpause(vm: str) -> None:
    run(["unpause", container_name(vm)])


def logs(vm: str, *, tail: int | None = None, since: str | None = None) -> str:
    args = ["logs"]
    if tail is not None:
        args += ["--tail", str(tail)]
    if since is not None:
        args += ["--since", since]
    args.append(container_name(vm))
    proc = run(args, check=False)
    return (proc.stdout or "") + (proc.stderr or "")


def follow_logs(vm: str, *, since: str | None = None) -> Iterator[str]:
    args = ["logs", "-f"]
    if since is not None:
        args += ["--since", since]
    args.append(container_name(vm))
    proc = subprocess.Popen(
        base_argv() + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    try:
        yield from proc.stdout
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_until(vm: str, states: set[str], *, timeout: int = 60, interval: float = 0.5) -> str:
    deadline = time.monotonic() + timeout
    current = "absent"
    while time.monotonic() < deadline:
        current = state(vm)
        if current in states:
            return current
        time.sleep(interval)
    return current


def all_containers() -> list[dict[str, Any]]:
    """Every vmhub-managed container, running or not."""
    proc = run(["ps", "-a", "--format", "{{.Names}}"], check=False)
    out: list[dict[str, Any]] = []
    for name in proc.stdout.split():
        if not name.startswith(CONTAINER_PREFIX):
            continue
        data = inspect(name[len(CONTAINER_PREFIX) :])
        if not data:
            continue
        labels = ((data.get("Config") or {}).get("Labels")) or {}
        if labels.get(LABEL_MANAGED) != "true":
            continue
        out.append(
            {
                "container": name,
                "vm": labels.get(LABEL_NAME) or name[len(CONTAINER_PREFIX) :],
                "state": (data.get("State") or {}).get("Status", "unknown"),
            }
        )
    return out


def orphans() -> list[dict[str, Any]]:
    """Managed containers whose VM directory no longer exists.

    Renaming a VM directory orphans its container: the container keeps the old
    name, keeps running, and nothing in vmhub will ever stop it again. The
    immutable label is what links the two, so it is what the check uses.
    """
    from . import registry

    known = set(registry.all_names())
    return [c for c in all_containers() if c["vm"] not in known]


def prune_orphans(*, keep_running: bool = False) -> list[str]:
    """Remove orphaned containers. rm() takes a VM name, not a container name."""
    removed: list[str] = []
    failed: list[str] = []
    for entry in orphans():
        if keep_running and entry["state"] == "running":
            continue
        container = entry["container"]
        try:
            rm(entry["vm"], force=True)
        except PodmanError as exc:
            failed.append(f"{container}: {exc}")
            continue
        if container_exists(container):
            failed.append(f"{container}: still present after rm")
            continue
        removed.append(f"{container} (was {entry['vm']}, {entry['state']})")
    if failed:
        raise PodmanError("; ".join(failed))
    return removed


def container_exists(container: str) -> bool:
    return run(["container", "exists", container], check=False).returncode == 0


def host_has_kvm() -> bool:
    try:
        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def is_rootless() -> bool:
    return run(["info", "--format", "{{.Host.Security.Rootless}}"]).stdout.strip() == "true"


def oci_runtime() -> str:
    return run(["info", "--format", "{{.Host.OCIRuntime.Name}}"]).stdout.strip()
