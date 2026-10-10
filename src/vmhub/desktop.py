"""A start-menu entry for the GUI.

Without one, vmhub is reachable only by remembering the checkout path and
typing it. This is a different thing from the session autostart file in
~/.config/autostart: that one starts the GUI when the desktop session
comes up, it does not put anything in the menu.

The entry names the checkout by absolute path, so moving or deleting the
checkout is what makes the entry stop working, rather than leaving a
launcher behind that fails when it is picked.

The icon is a file in the checkout that gets copied into the VM owner's
~/.local/share/icons, beside the entry. Both go to the owner and not the
caller, for the same reason as the entry itself.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import paths
from .autostart import _owner_home, gui_path, vmctl_path
from .errors import LifecycleError

DESKTOP_FILE_NAME = "vmhub.desktop"

ICON_NAME = "vmhub"
ICON_SOURCE_RELATIVE = Path("src") / "vmhub" / "icons" / f"{ICON_NAME}.svg"


def applications_dir() -> Path:
    """Where the menu looks for per-user entries.

    Under the VM owner's home, not the caller's: `sudo vmctl desktop install`
    otherwise writes the entry into root's menu, where the person who
    actually uses the VMs will never see it.
    """
    return _owner_home() / ".local" / "share" / "applications"


def desktop_path() -> Path:
    return applications_dir() / DESKTOP_FILE_NAME


def icon_theme_dir() -> Path:
    """The user's hicolor theme.

    hicolor is the theme every desktop falls back to, so an icon here is
    found whatever icon theme is set. The cache file lives in the theme
    root, not in the scalable/apps subdirectory.
    """
    return _owner_home() / ".local" / "share" / "icons" / "hicolor"


def icon_dir() -> Path:
    return icon_theme_dir() / "scalable" / "apps"


def icon_path() -> Path:
    return icon_dir() / f"{ICON_NAME}.svg"


def icon_source() -> Path:
    """The icon as it ships in the checkout."""
    return paths.project_root() / ICON_SOURCE_RELATIVE


def entry_text() -> str:
    """The .desktop file as it should be on disk.

    Icon is a bare theme name rather than a path. That is the portable
    spelling: the menu finds it in hicolor, and if the icon is somehow
    missing it falls back to a generic application icon instead of
    pointing at a file that is not there.
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
            f"Icon={ICON_NAME}",
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


def _refresh_icon_cache() -> None:
    """Rebuild the hicolor index so a newly copied icon is found.

    -f because the cache is normally written at package-install time by
    root and the per-user copy may not exist yet; -t because there is no
    index.theme here to validate against.
    """
    tool = shutil.which("gtk-update-icon-cache")
    if tool:
        subprocess.run(
            [tool, "-f", "-t", str(icon_theme_dir())], capture_output=True, check=False
        )


def install() -> list[str]:
    if not gui_path().is_file():
        raise LifecycleError(
            f"no GUI launcher at {gui_path()}; run this from the vmhub checkout"
        )
    source = icon_source()
    if not source.is_file():
        # Refuse rather than write an entry whose Icon= names nothing: that
        # is the broken-placeholder case this replaced.
        raise LifecycleError(f"the icon is missing from the checkout at {source}")

    text = entry_text()
    problem = _validate(text)
    if problem:
        raise LifecycleError(f"the generated .desktop file is invalid: {problem}")

    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, icon)
    _refresh_icon_cache()

    target = desktop_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _refresh_menu_cache(target.parent)
    return [
        f"wrote {target} (vmhub now appears in your application menu)",
        f"wrote {icon} (the icon the menu shows for it)",
    ]


def uninstall() -> list[str]:
    lines: list[str] = []
    icon = icon_path()
    if icon.is_file():
        icon.unlink()
        _refresh_icon_cache()
        lines.append(f"removed {icon}")
    target = desktop_path()
    if target.is_file():
        target.unlink()
        _refresh_menu_cache(target.parent)
        lines.append(f"removed {target}")
    if not lines:
        return [f"nothing to remove at {target}"]
    return lines


def status() -> dict[str, object]:
    target = desktop_path()
    icon = icon_path()
    return {
        "menu_entry": target.is_file(),
        "path": str(target),
        "icon": str(icon),
        "icon_installed": icon.is_file(),
        "icon_source": str(icon_source()),
        "gui_launcher": gui_path().is_file(),
        "vmctl": vmctl_path().is_file(),
        "validate_tool": shutil.which("desktop-file-validate") or "missing",
        "menu_cache_tool": shutil.which("update-desktop-database") or "missing",
        "icon_cache_tool": shutil.which("gtk-update-icon-cache") or "missing",
    }