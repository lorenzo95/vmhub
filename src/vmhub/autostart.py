from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import paths
from .errors import LifecycleError

SYSTEMD_UNIT_NAME = "vmhub.service"
SYSVINIT_SCRIPT = "/etc/init.d/vmhub"
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "vmhub.desktop"
LINGER_HINT = (
    "systemd user services stop when you log out unless lingering is enabled.\n"
    "  sudo loginctl enable-linger $USER"
)


def vmctl_path() -> Path:
    return paths.project_root() / "vmctl"


def gui_path() -> Path:
    return paths.project_root() / "vmhub"


def detect_init() -> str:
    if os.path.isdir("/run/systemd/system"):
        return "systemd"
    if Path("/sbin/init").exists() or Path("/etc/init.d").is_dir():
        return "sysvinit"
    return "none"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _vm_owner() -> str:
    """The user who owns the VMs.

    Installing with sudo must not bake in "root": rootless podman keeps its
    store under the user's home, so running as root at boot would start nothing.
    """
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or _login_user()


def _login_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "root"


def _render(template: str) -> str:
    return (
        template.replace("@VMCTL@", str(vmctl_path()))
        .replace("@GUI@", str(gui_path()))
        .replace("@README@", str(paths.project_root() / "README.md"))
        .replace("@OWNER@", _vm_owner())
    )


def systemd_unit_text() -> str:
    return _render(
        """[Unit]
Description=vmhub - restore previously running VMs
Documentation=file://@README@
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=@VMCTL@ restore
ExecStop=@VMCTL@ stop-all --quiet

[Install]
WantedBy=default.target
"""
    )


def sysvinit_script_text() -> str:
    return _render(
        """#!/bin/sh
### BEGIN INIT INFO
# Provides:          vmhub
# Required-Start:    $remote_fs
# Required-Stop:     $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Restore previously running vmhub VMs
### END INIT INFO
VMCTL=@VMCTL@
OWNER="@OWNER@"

# Run as the VM owner, not as root: rootless podman keeps its containers under
# the owner's home and XDG_RUNTIME_DIR, so `id -un` here would be root at boot.
run_as_owner() {
    uid=$(id -u "$OWNER" 2>/dev/null) || {
        echo "vmhub: user '$OWNER' does not exist" >&2
        return 1
    }
    su - "$OWNER" -c "XDG_RUNTIME_DIR=/run/user/$uid $*"
}

case "$1" in
  start)
    echo "vmhub: restoring VMs as $OWNER"
    run_as_owner "$VMCTL restore" || true
    ;;
  stop)
    echo "vmhub: stopping VMs"
    run_as_owner "$VMCTL stop-all --quiet" || true
    ;;
  restart|reload)
    "$0" stop
    "$0" start
    ;;
  status)
    run_as_owner "$VMCTL ls --quiet" || true
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
exit 0
"""
    )


def desktop_entry_text() -> str:
    return _render(
        """[Desktop Entry]
Type=Application
Name=vmhub
Comment=Manage podman-hosted QEMU VMs
Exec=@GUI@
Terminal=false
Categories=System;Emulator;
X-GNOME-Autostart-enabled=true
"""
    )


def install(*, target: str | None = None, gui: bool = False) -> list[str]:
    chosen = target or detect_init()
    done: list[str] = []

    if chosen == "systemd":
        unit = _systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit_text())
        done.append(f"wrote {unit}")
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            result = subprocess.run(
                ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                done.append("enabled and started vmhub.service")
            else:
                done.append(
                    "could not enable automatically: "
                    + (result.stderr or result.stdout).strip().splitlines()[-1:]
                )
                done.append(f"run manually: systemctl --user enable --now {SYSTEMD_UNIT_NAME}")
        else:
            done.append("systemctl not found; unit written but not enabled")
        done.append(LINGER_HINT)

    elif chosen == "sysvinit":
        script = Path(SYSVINIT_SCRIPT)
        if os.geteuid() == 0:
            script.write_text(sysvinit_script_text())
            script.chmod(0o755)
            done.append(f"wrote {script}")
            if shutil.which("update-rc.d"):
                subprocess.run(["update-rc.d", "vmhub", "defaults"], check=False)
                done.append("registered with update-rc.d")
            if shutil.which("service"):
                subprocess.run(["service", "vmhub", "start"], check=False)
                done.append("started via service vmhub start")
        else:
            raise LifecycleError(
                f"writing {script} needs root. Re-run with sudo, or install a\n"
                f"session autostart entry instead:\n"
                f"    {vmctl_path()} autostart install --target session"
            )

    elif chosen == "session":
        done.extend(_install_session())
        return done

    else:
        raise LifecycleError(
            "could not detect an init system. Install explicitly with "
            "--target systemd|sysvinit|session"
        )

    if gui:
        done.extend(_install_session())
    return done


def _install_session() -> list[str]:
    AUTOSTART_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_DESKTOP.write_text(desktop_entry_text())
    return [f"wrote {AUTOSTART_DESKTOP} (GUI starts with your desktop session)"]


def uninstall(*, target: str | None = None) -> list[str]:
    chosen = target or detect_init()
    done: list[str] = []

    if chosen == "systemd":
        unit = _systemd_unit_path()
        if unit.is_file():
            if shutil.which("systemctl"):
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
                    capture_output=True,
                    check=False,
                )
            unit.unlink()
            if shutil.which("systemctl"):
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            done.append(f"removed {unit}")
        else:
            done.append(f"nothing to remove at {unit}")
    elif chosen == "sysvinit":
        script = Path(SYSVINIT_SCRIPT)
        if script.is_file():
            if shutil.which("update-rc.d"):
                subprocess.run(["update-rc.d", "-f", "vmhub", "remove"], check=False)
            script.unlink()
            done.append(f"removed {script}")
        else:
            done.append(f"nothing to remove at {script}")
    else:
        done.append(f"nothing to remove for target {chosen}")

    if AUTOSTART_DESKTOP.is_file():
        AUTOSTART_DESKTOP.unlink()
        done.append(f"removed {AUTOSTART_DESKTOP}")
    return done


def status() -> dict[str, object]:
    chosen = detect_init()
    result: dict[str, object] = {
        "init": chosen,
        "systemd_unit": _systemd_unit_path().is_file(),
        "sysvinit_script": Path(SYSVINIT_SCRIPT).is_file(),
        "session_autostart": AUTOSTART_DESKTOP.is_file(),
        "vmctl": vmctl_path().is_file(),
        "gui": gui_path().is_file(),
    }
    if chosen == "systemd" and shutil.which("systemctl"):
        proc = subprocess.run(
            ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME],
            capture_output=True,
            text=True,
        )
        result["systemd_enabled"] = (proc.stdout or proc.stderr).strip()
    return result
