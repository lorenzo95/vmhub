"""A start-menu entry for the GUI.

Without one, vmhub is reachable only by remembering the checkout path and
typing it. This is a different thing from the session autostart file in
~/.config/autostart: that one starts the GUI when the desktop session
comes up, it does not put anything in the menu.

The entry names the checkout by absolute path, so moving or deleting the
checkout is what makes the entry stop working, rather than leaving a
launcher behind that fails when it is picked.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .autostart import _owner_home, gui_path, vmctl_path
from .errors import LifecycleError

DESKTOP_FILE_NAME = "vmhub.desktop"


def applications_dir() -> Path:
    """Where the menu looks for per-user entries.

    Under the VM owner's home, not the caller's: `sudo vmctl desktop install`
    otherwise writes the entry into root's menu, where the person who
    actually uses the VMs will never see it.
    """
    return _owner_home() / ".local" / "share" / "applications"


def desktop_path() -> Path:
    return applications_dir() / DESKTOP_FILE_NAME


def entry_text() -> str:
    """The .desktop file as it should be on disk.

    There is deliberately no Icon= line. The tree has no icon yet, and an
    Icon= naming a file that is not there gets the menu to show a broken
    placeholder, which reads worse than the generic icon a missing Icon=
    produces.
    """
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=vmhub",
            "GenericName=Virtual machines",
            "Comment=Manage QEMU VMs running in rootless podman containers",
            f"Exec={gui_path()}",
            # vmctl rather than the GUI script: it is the one that has to
            # exist for the checkout to be usable at all.
            f"TryExec={vmctl_path()}",
            "Terminal=false",
            "StartupNotify=true",
            "Categories=System;Emulator;",
            "Keywords=vm;qemu;podman;virtual machine;",
            "",
        ]
    )


def _validate(text: str) -> str | None:
    """Run desktop-file-validate over the entry. Returns a complaint, or None.

    Worth the subprocess: the entry is generated, so a mistake here is a
    file the menu silently ignores.
    """
    tool = shutil.which("desktop-file-validate")
    if not tool:
        return None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".desktop", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
        staged = Path(handle.name)
    try:
        proc = subprocess.run([tool, str(staged)], capture_output=True, text=True)
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or "").strip()
    finally:
        staged.unlink(missing_ok=True)
    return None


def _refresh_menu_cache(directory: Path) -> None:
    """Tell the desktop that the entry list changed."""
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, str(directory)], capture_output=True, check=False)


def install() -> list[str]:
    if not gui_path().is_file():
        raise LifecycleError(
            f"no GUI launcher at {gui_path()}; run this from the vmhub checkout"
        )
    text = entry_text()
    problem = _validate(text)
    if problem:
        raise LifecycleError(f"the generated .desktop file is invalid: {problem}")

    target = desktop_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _refresh_menu_cache(target.parent)
    return [f"wrote {target} (vmhub now appears in your application menu)"]


def uninstall() -> list[str]:
    target = desktop_path()
    if not target.is_file():
        return [f"nothing to remove at {target}"]
    target.unlink()
    _refresh_menu_cache(target.parent)
    return [f"removed {target}"]


def status() -> dict[str, object]:
    target = desktop_path()
    return {
        "menu_entry": target.is_file(),
        "path": str(target),
        "gui_launcher": gui_path().is_file(),
        "vmctl": vmctl_path().is_file(),
        "validate_tool": shutil.which("desktop-file-validate") or "missing",
        "menu_cache_tool": shutil.which("update-desktop-database") or "missing",
    }