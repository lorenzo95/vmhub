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
        .replace("@PROJECTDIR@", str(paths.project_root()))
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
# systemd's default working directory is "/", and project_root() only walks
# upward from the cwd -- so without this the unit failed with "not a vmhub
# project root: /" on exactly the machines where it is installed.
WorkingDirectory=@PROJECTDIR@
Environment=VMHUB_HOME=@PROJECTDIR@
ExecStart=@VMCTL@ restore
ExecStop=@VMCTL@ stop-all --quiet --preserve-run-state

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
PROJECT="@PROJECTDIR@"

# Run as the VM owner, not as root: rootless podman keeps its containers under
# the owner's home and XDG_RUNTIME_DIR, so `id -un` here would be root at boot.
#
# `su -` gives a LOGIN SHELL in $OWNER's home. project_root() only walks
# upward from the cwd, so from /home/admin it can never reach
# /home/admin/vmhub, which is below it, and its single fallback is hardcoded
# to ~/vms. The hook therefore died with "not a vmhub project root:
# /home/admin" on every boot. Both the cd and VMHUB_HOME below are there so
# the working directory cannot matter.
#
# /run/user/$uid does not exist yet when this runs. /run is a tmpfs that init
# populates, and the per-user directory under it is created by the first login
# -- which has not happened at runlevel 5 on a machine nobody has sat down at.
# Rootless podman cannot even read its own configuration without it, and says
# so instead of saying why: "Failed to obtain podman configuration: lstat
# /run/user: no such file or directory". restore_run_state() catches that per
# VM, prints "could not restore winni: ..." next to "nothing to restore", and
# the machine comes up with every VM down and no reason on the console.
#
# Creating it here is what pam_systemd/logind would do at login, and it is
# idempotent: if the user is already logged in the directory is there and this
# is a no-op.
ensure_runtime_dir() {
    [ -d "/run/user/$1" ] && return 0
    mkdir -p "/run/user/$1" 2>/dev/null || return 1
    chown "$OWNER" "/run/user/$1" 2>/dev/null || return 1
    chmod 700 "/run/user/$1" 2>/dev/null || return 1
    return 0
}

# Say everything twice: to the console, and to syslog under the "vmhub" tag.
#
# The console is not a record. /var/log/boot.log on this host stops being
# written about five seconds into the boot -- long before rsyslogd is up -- so
# a hook that runs at +5s and prints a failure leaves no trace anywhere on the
# machine. rsyslogd starts around +7s, so `logger` from inside the hook lands
# in /var/log/syslog and survives the boot. Without this, every question about
# "did it run, and what did it say" has to be answered with another reboot and
# a marker file.
log() {
    echo "vmhub: $*"
    logger -t vmhub -- "$*" 2>/dev/null
}

# Wait for /dev/kvm to be usable by $OWNER before starting anything.
#
# udev creates /dev/kvm early in the boot and applies the owner's access ACL a
# couple of seconds later. QEMU without KVM does not boot these VMs: it starts,
# prints "KVM acceleration is not available (/dev/kvm is unwriteable)", and the
# container is gone about a tenth of a second later. This hook fires at boot+5s
# and loses that race every time, and it looks exactly like "the VM did not come
# back". Checking beats retrying here, because the condition is knowable and
# resolves itself within seconds -- the retry loop below was only ever masking
# it, and on a slower boot it can burn all five attempts and still lose.
#
# A host with no /dev/kvm at all is not waiting for anything, so return at once
# rather than adding 30s to every boot on a machine that will never have one.
wait_for_kvm() {
    [ -e /dev/kvm ] || return 0
    waited=0
    while [ "$waited" -lt 30 ]; do
        if su - "$OWNER" -c "test -r /dev/kvm -a -w /dev/kvm" 2>/dev/null; then
            return 0
        fi
        waited=$((waited + 1))
        sleep 1
    done
    return 1
}

run_as_owner() {
    uid=$(id -u "$OWNER" 2>/dev/null) || {
        log "user '$OWNER' does not exist"
        return 1
    }
    ensure_runtime_dir "$uid" || log "could not create /run/user/$uid"
    out=$(su - "$OWNER" -c "cd \"$PROJECT\" && VMHUB_HOME=\"$PROJECT\" XDG_RUNTIME_DIR=/run/user/$uid $*" 2>&1)
    rc=$?
    if [ -n "$out" ]; then
        echo "$out"
        logger -t vmhub -- "$out" 2>/dev/null
    fi
    log "$1 -> exit $rc"
    return $rc
}

# No `|| true` anywhere below, and the case's status is propagated. Every
# branch used to swallow its failure and exit 0, so a hook that did nothing at
# all reported success -- which is what made this take a reboot and a report
# to find.
#
# The stop branch keeps last_run_state, because stopping the VMs at shutdown is
# what the host forces on us, not something you asked for -- and that flag is
# the only thing the start branch above has to go on. Writing "stopped" to it
# is exactly how this hook used to defeat itself: K01vmhub stopped every VM,
# each stop recorded last_run_state=stopped, and the next boot then reported
# "nothing to restore" and left everything down. The guests still get their
# ACPI shutdown; only the bookkeeping survives it.
case "$1" in
  start)
    log "restoring VMs as $OWNER"
    # Rootless podman and QEMU both need the boot to have caught up first; see
    # ensure_runtime_dir and wait_for_kvm. These two waits are why this hook
    # works, and the retry loop is the backstop for whatever is left.
    uid=$(id -u "$OWNER" 2>/dev/null) && ensure_runtime_dir "$uid"
    wait_for_kvm || log "/dev/kvm still not usable by $OWNER after 30s; VMs may fail to start"
    attempt=1
    while [ "$attempt" -le 5 ]; do
        if run_as_owner "$VMCTL restore"; then
            break
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -le 5 ]; then
            log "restore attempt $((attempt - 1)) left VMs down; retrying in 10s"
            sleep 10
        fi
    done
    if [ "$attempt" -gt 5 ]; then
        log "gave up after 5 attempts; some VMs are still down"
        exit 1
    fi
    ;;
  stop)
    log "stopping VMs"
    run_as_owner "$VMCTL stop-all --quiet --preserve-run-state"
    ;;
  restart|reload)
    "$0" stop
    "$0" start
    ;;
  status)
    run_as_owner "$VMCTL ls --quiet"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
exit $?
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


def _enable_failure(result: subprocess.CompletedProcess) -> str:
    """Explain a failed `systemctl --user enable` in one readable line.

    Kept separate so it can be tested without touching systemctl. The
    obvious one-liner here is a str + list TypeError, which is what this
    used to be: it fired on every host with no systemd user session, so
    exactly the machines most likely to hit it never saw the advice.
    """
    detail = (result.stderr or result.stdout).strip().splitlines()
    return "could not enable automatically: " + (
        detail[-1] if detail else "systemctl gave no explanation"
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
                done.append(_enable_failure(result))
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
