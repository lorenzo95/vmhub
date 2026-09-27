from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "VMHUB_HOME"
DEFAULT_HOME = "~/vms"

BLUEPRINTS_DIR = "blueprints"
SYSTEMD_DIR = "systemd"
SYSVINIT_DIR = "sysvinit"
DISK_SUBDIR = "disk"
STORAGE_SUBDIR = "storage"
SPEC_FILE = "vm.toml"
META_FILE = "meta.json"

DISK_NAME = "data.qcow2"
DISK_SHADOW = "/storage/data.qcow2"
QMP_SOCKET = "qmp.sock"
QGA_SOCKET = "qga.sock"
MONITOR_SOCKET = "monitor.sock"
NVRAM_STEM = "uefi"
DRIVERS_ISO = "drivers.iso"
START_ISO = "start.iso"

PROJECT_MARKERS = ("systemd", "sysvinit", "blueprints")


def project_root() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        root = Path(env).expanduser().resolve()
    else:
        root = _discover(Path.cwd())
    if not (root / SYSTEMD_DIR).is_dir():
        raise FileNotFoundError(f"not a vmhub project root: {root}")
    return root


def _discover(start: Path) -> Path:
    candidate = start
    for candidate in (start, *start.parents):
        if all((candidate / marker).is_dir() for marker in PROJECT_MARKERS):
            return candidate
    fallback = Path(DEFAULT_HOME).expanduser()
    if (fallback / SYSTEMD_DIR).is_dir():
        return fallback
    return candidate


def vm_dir(vm: str) -> Path:
    return project_root() / vm


def spec_path(vm: str) -> Path:
    return vm_dir(vm) / SPEC_FILE


def meta_path(vm: str) -> Path:
    return vm_dir(vm) / META_FILE


def storage_dir(vm: str) -> Path:
    return vm_dir(vm) / STORAGE_SUBDIR


def disk_dir(vm: str) -> Path:
    return vm_dir(vm) / DISK_SUBDIR


def disk_path(vm: str) -> Path:
    return disk_dir(vm) / DISK_NAME


def qmp_path(vm: str) -> Path:
    return storage_dir(vm) / QMP_SOCKET


def blueprints_dir() -> Path:
    return project_root() / BLUEPRINTS_DIR


def backup_dir() -> Path:
    root = project_root() / "backups"
    root.mkdir(exist_ok=True)
    return root


