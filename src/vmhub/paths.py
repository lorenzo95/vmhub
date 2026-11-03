from __future__ import annotations

import os
from pathlib import Path

from .errors import ProjectRootError

ENV_HOME = "VMHUB_HOME"
DEFAULT_HOME = "~/vmhub"
LEGACY_HOME = "~/vms"

BLUEPRINTS_DIR = "blueprints"
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
SERIAL_SOCKET = "serial.sock"
SEED_ISO = "seed.iso"
SEED_SUBDIR = "seed"
IMAGE_CACHE_ENV = "VMHUB_IMAGE_CACHE"

# Detection is by content, not by directory name: both markers are tracked in
# git, so a fresh clone resolves. The empty systemd/ and sysvinit/ directories
# never survive a clone, so depending on them broke every command.
PROJECT_MARKER_DIRS = (BLUEPRINTS_DIR,)
PROJECT_MARKER_FILES = ("vmctl",)


def _is_project_root(path: Path) -> bool:
    return all((path / name).is_dir() for name in PROJECT_MARKER_DIRS) and all(
        (path / name).is_file() for name in PROJECT_MARKER_FILES
    )


def project_root() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        root = Path(env).expanduser().resolve()
    else:
        root = _discover(Path.cwd())
    if not _is_project_root(root):
        raise ProjectRootError(
            f"not a vmhub project root: {root} "
            f"(expected a directory containing blueprints/ and vmctl)"
        )
    return root


def _expand_home(fallback: str) -> Path:
    """Where a convention says the checkout might be.

    Its own function so a test can point the convention at a temp tree without
    monkeypatching HOME, which expanduser() only consults on some paths.
    """
    return Path(fallback).expanduser()


def _discover(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if _is_project_root(candidate):
            return candidate
    # The documented convention, not a requirement: honour it so a user
    # running vmctl from an unrelated directory still finds the checkout.
    # ~/vmhub is what `git clone` actually produces, and it is the README's
    # convention throughout; ~/vms is what the README used to say, and an
    # install set up against that is still somebody's working setup.
    for convention in (DEFAULT_HOME, LEGACY_HOME):
        fallback = _expand_home(convention)
        if _is_project_root(fallback):
            return fallback
    # Return where the user actually was: the filesystem root is a far more
    # confusing thing to report back than the directory they ran the command in.
    return start


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


def serial_path(vm: str) -> Path:
    return storage_dir(vm) / SERIAL_SOCKET


def seed_dir(vm: str) -> Path:
    return vm_dir(vm) / SEED_SUBDIR


def image_cache_dir() -> Path:
    """Where downloaded cloud images live.

    Outside the checkout on purpose: a Debian cloud image is ~400 MB, and
    several of them next to the VMs would be copied into every `git status`
    and every backup of the project tree. VMHUB_IMAGE_CACHE exists for the
    test suite and for people who keep a big cache on another filesystem.
    """
    override = os.environ.get(IMAGE_CACHE_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
        path = Path(base).expanduser() / "vmhub" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def blueprints_dir() -> Path:
    return project_root() / BLUEPRINTS_DIR


def backup_dir() -> Path:
    root = project_root() / "backups"
    root.mkdir(exist_ok=True)
    return root


