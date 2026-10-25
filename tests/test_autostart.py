"""The boot hook has to work from a directory it did not choose.

`su -` gives a login shell in the VM owner's home. project_root() only walks
upward from the cwd, so from ~/ it can never reach ~/vmhub, which is below
it, and its single fallback is hardcoded to ~/vms. The hook therefore failed
on every boot -- and because each branch ended in `|| true` followed by
`exit 0`, it failed *successfully*, which is why it took a reboot and a
report to find.

The second half of the file covers the other way this hook could fail to
work: run perfectly, and have nothing to do. `restore_run_state` starts only
the VMs whose meta says `last_run_state == "running"`, and a plain `stop`
rewrites that to "stopped". The hook's own stop branch runs at shutdown, so
it used to overwrite the record of what was running and then start, at the
next boot, with an empty list -- reporting "nothing to restore" and leaving
every VM down.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from vmhub import autostart, registry

# The package re-exports the public lifecycle API; _mark_stopped is private to
# the inner module, and stop_all's globals resolve there too, so patching the
# package attribute would not affect what stop_all actually calls.
from vmhub.lifecycle import lifecycle as lc


def _code_lines(text: str) -> list[str]:
    """Drop comments: a comment may mention `|| true` while no code uses it."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def test_sysvinit_hook_runs_inside_the_project() -> None:
    text = autostart.sysvinit_script_text()
    code = "\n".join(_code_lines(text))
    # Either one is enough to make the cwd irrelevant; both are present so a
    # login shell, systemd's "/", or cron cannot matter.
    assert "cd " in code and "PROJECT" in code, code
    assert "VMHUB_HOME" in code, code


def test_sysvinit_hook_does_not_swallow_failures() -> None:
    """A hook that fails must not exit 0, or it reports success doing nothing."""
    text = autostart.sysvinit_script_text()
    code = "\n".join(_code_lines(text))
    assert "|| true" not in code, code
    assert "exit $?" in code, code
    assert "exit 0" not in code, code


def test_sysvinit_hook_runs_as_the_vm_owner_not_root() -> None:
    """Rootless podman keeps its store under the owner, so root starts nothing."""
    text = autostart.sysvinit_script_text()
    assert 'OWNER="@OWNER@"' not in text, "placeholder left unsubstituted"
    assert "su - " in text or "su -c" in text, text


def test_systemd_unit_also_knows_the_project() -> None:
    """systemd's default working directory is /, which fails the same way."""
    text = autostart.systemd_unit_text()
    assert "WorkingDirectory=" in text, text
    assert "VMHUB_HOME=" in text, text


def test_the_session_entry_starts_the_gui_where_it_can_find_the_project() -> None:
    """A login session runs this in the user's home, and project_root() only
    walks upward from the cwd -- so from /home/you it cannot reach
    /home/you/vmhub. Same reason the menu entry carries Path=."""
    text = autostart.desktop_entry_text()
    assert "Path=" in text, text
    assert "@PROJECTDIR@" not in text, "placeholder left unsubstituted"


def test_the_session_entry_binds_itself_to_the_window_it_starts() -> None:
    """Without StartupWMClass the session window has no entry to match, so
    it gets a generic icon and nothing stable to pin."""
    text = autostart.desktop_entry_text()
    assert f"StartupWMClass={autostart.WM_CLASS}" in text, text


def test_both_templates_name_a_real_project() -> None:
    """@PROJECTDIR@ must render, or the hook points at the literal placeholder."""
    import os
    from pathlib import Path

    os.environ["VMHUB_HOME"] = str(
        Path(__file__).resolve().parent.parent
    )
    try:
        for text in (autostart.sysvinit_script_text(), autostart.systemd_unit_text()):
            assert "@PROJECTDIR@" not in text, text
            assert "@VMCTL@" not in text, text
            assert "@OWNER@" not in text, text
    finally:
        os.environ.pop("VMHUB_HOME", None)


def _running_vm(root: Path, name: str = "demo") -> str:
    """A VM on disk whose meta says it was running when we last looked."""
    vm_dir = root / name
    vm_dir.mkdir(parents=True, exist_ok=True)
    (vm_dir / "meta.json").write_text(
        json.dumps({"uuid": "u", "mac": "m", "last_run_state": "running"})
    )
    return name


def _run_state(name: str) -> str:
    return registry.load_meta(name).last_run_state


def test_a_shutdown_stop_leaves_the_boot_restore_signal_alone(project_home: Path) -> None:
    """The whole point: stop the container, keep the record of it."""
    vm = _running_vm(project_home)
    lc._mark_stopped(vm, preserve_run_state=True)
    assert _run_state(vm) == "running"


def test_a_stop_the_user_asked_for_still_records_stopped(project_home: Path) -> None:
    """The fix must not leak into ordinary use -- `vmctl stop` means stop."""
    vm = _running_vm(project_home)
    lc._mark_stopped(vm)
    assert _run_state(vm) == "stopped"


def test_stop_all_forwards_preserve_run_state(monkeypatch) -> None:
    """stop_all is what the shutdown branch calls, so the flag has to reach stop()."""
    seen: list[bool] = []
    monkeypatch.setattr(
        lc, "list_vms", lambda: [SimpleNamespace(name="demo", powered=True)]
    )
    monkeypatch.setattr(
        lc, "stop", lambda vm, **kw: seen.append(kw.get("preserve_run_state", False)),
    )

    lc.stop_all(preserve_run_state=True)
    assert seen == [True]

    seen.clear()
    lc.stop_all()
    assert seen == [False]


def test_both_templates_stop_without_cancelling_the_next_boot() -> None:
    """A hook whose stop branch drops the flag silently disables itself."""
    sysv = "\n".join(_code_lines(autostart.sysvinit_script_text()))
    assert "stop-all --quiet --preserve-run-state" in sysv, sysv

    unit = "\n".join(_code_lines(autostart.systemd_unit_text()))
    assert "stop-all --quiet --preserve-run-state" in unit, unit


def test_sysvinit_hook_creates_the_runtime_dir_podman_needs() -> None:
    """At boot /run/user/<uid> does not exist, and rootless podman then fails
    every single call with 'Failed to obtain podman configuration: lstat
    /run/user: no such file or directory' -- which restore_run_state() reports
    per VM and then calls 'nothing to restore'. The VM owner is not logged in
    yet at runlevel 5, so nothing else will create it."""
    code = "\n".join(_code_lines(autostart.sysvinit_script_text()))
    assert "mkdir -p" in code, code
    assert "chown" in code, code
    assert "chmod 700" in code, code
    # It has to happen before the su that runs podman, not after.
    assert code.index("mkdir -p") < code.index("su - "), code


def test_sysvinit_hook_retries_because_boot_is_not_ready_yet() -> None:
    """Rootless podman is unusable seconds into a boot and fine minutes later.
    Without a retry the hook reports a clean start and leaves every VM down,
    which is exactly the failure it was installed to prevent."""
    code = "\n".join(_code_lines(autostart.sysvinit_script_text()))
    assert "restore" in code, code
    # A bounded loop, and it has to give up rather than spin forever.
    assert "while [ \"$attempt\" -le 5 ]" in code, code
    assert "sleep 10" in code, code
    assert "exit 1" in code, code


def test_sysvinit_hook_writes_to_syslog_not_just_the_console() -> None:
    """boot.log stops being written ~5s into this host's boot and rsyslogd
    starts ~7s in, so a hook running at +5s that only prints leaves no trace
    anywhere. That is what made this take two rounds of guessing."""
    code = "\n".join(_code_lines(autostart.sysvinit_script_text()))
    assert "logger -t vmhub" in code, code
    # and it must log what the command actually said, not just that it ran
    assert 'logger -t vmhub -- "$out"' in code, code


def test_sysvinit_hook_waits_for_kvm_before_restoring() -> None:
    """udev creates /dev/kvm at boot and applies the owner's ACL seconds later.
    QEMU without KVM starts, prints 'KVM acceleration is not available', and
    the container is dead 0.1s later -- which reads as 'the VM did not come
    back'. The hook runs at boot+5s and loses that race every time."""
    code = "\n".join(_code_lines(autostart.sysvinit_script_text()))
    assert "wait_for_kvm" in code, code
    assert "test -r /dev/kvm -a -w /dev/kvm" in code, code
    # ...and must not sit waiting on a host that has no KVM at all.
    assert "[ -e /dev/kvm ] || return 0" in code, code
    # it has to happen before the restore loop
    assert code.index("wait_for_kvm || log") < code.index("attempt=1"), code


def test_restore_never_starts_the_test_harness(monkeypatch) -> None:
    """An interrupted test run leaves harness VMs in the registry looking like
    VMs somebody left running. Restoring one wastes the boot hook's retry budget
    and, if it has a stale host port, fails every attempt -- so the hook reports
    'gave up after 5 attempts' while the user's own VMs are actually fine."""
    started: list[str] = []

    def fake_metas():
        for name in ("vmhub-selftest", "vmhub-selftest-full", "vmhub-guicheck-live",
                     "sweepvm", "winni"):
            yield SimpleNamespace(name=name, last_run_state="running")

    monkeypatch.setattr(lc.registry, "iter_metas", fake_metas)
    monkeypatch.setattr(lc.podman, "is_running", lambda vm: False)
    monkeypatch.setattr(lc, "start", lambda vm, progress=None: started.append(vm))

    assert lc.restore_run_state() == ["winni"]
    assert started == ["winni"]


def test_install_enables_lingering_instead_of_only_hinting_at_it(monkeypatch) -> None:
    """The unit enables and starts perfectly and still does nothing at boot.

    systemd starts a user's manager on login and tears it down at logout, so
    with lingering off there is no user manager at boot at all. The symptom is
    a machine that came up fine days ago and simply stopped restoring.
    """
    from types import SimpleNamespace

    calls: list[list[str]] = []
    monkeypatch.setattr(
        autostart.shutil, "which", lambda name: "/usr/bin/loginctl" if name == "loginctl" else None
    )
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *a, **k: (calls.append(list(a[0])) or SimpleNamespace(returncode=0, stdout="Linger=yes", stderr="")),
    )
    monkeypatch.setattr(autostart, "_systemd_unit_path", lambda: Path(autostart.__file__))

    line = autostart._enable_linger()
    assert ["loginctl", "enable-linger", "admin"] in calls, calls
    assert "runs at boot" in line, line


def test_install_says_plainly_when_lingering_could_not_be_enabled(monkeypatch) -> None:
    """A hint that scrolls past is not the same as a warning that will not work."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        autostart.shutil, "which", lambda name: "/usr/bin/loginctl" if name == "loginctl" else None
    )
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Access denied"),
    )
    line = autostart._enable_linger()
    assert "NOT come back at boot" in line, line
    assert "sudo loginctl enable-linger" in line, line


def test_unit_goes_to_the_vm_owners_home_not_roots(monkeypatch) -> None:
    """`sudo vmctl autostart install` sets HOME=/root. Path.home() then writes
    the unit into /root/.config/systemd/user/ and enables it in root's user
    manager, while OWNER=admin is baked into the unit. Every message says it
    worked and nothing happens at boot -- which is what it did."""
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    monkeypatch.setattr(autostart.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir="/home/admin"))
    assert autostart._systemd_unit_path() == Path("/home/admin/.config/systemd/user/vmhub.service")

    # ...and it must follow the owner, not the caller's HOME
    monkeypatch.setattr(autostart.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir="/home/somebody-else"))
    assert "/home/somebody-else" in str(autostart._session_autostart_path())


def test_systemctl_user_calls_go_to_the_owners_manager(monkeypatch) -> None:
    """Under sudo, `systemctl --user` is root's user manager -- so enabling the
    unit there reports success and never runs it. Drop to the owner, and pass
    XDG_RUNTIME_DIR through env because sudo's env_reset strips it."""
    calls: list[list[str]] = []
    monkeypatch.setattr(autostart.os, "geteuid", lambda: 0)
    monkeypatch.setattr(autostart, "_vm_owner", lambda: "admin")
    monkeypatch.setattr(autostart.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=1000))
    monkeypatch.setattr(
        autostart.subprocess,
        "run",
        lambda *a, **k: (calls.append(list(a[0])) or SimpleNamespace(returncode=0, stdout="", stderr="")),
    )

    autostart._user_systemctl(["enable", "--now", "vmhub.service"])
    assert calls[0][:4] == ["sudo", "-u", "admin", "env"], calls[0]
    assert "XDG_RUNTIME_DIR=/run/user/1000" in calls[0], calls[0]
    assert calls[0][-4:] == ["--user", "enable", "--now", "vmhub.service"], calls[0]

    # already the owner: no sudo dance, no runtime dir games
    monkeypatch.setattr(autostart.os, "geteuid", lambda: 1000)
    autostart._user_systemctl(["daemon-reload"])
    assert calls[1] == ["systemctl", "--user", "daemon-reload"], calls[1]


def test_restore_waits_for_the_boot_to_be_ready(monkeypatch) -> None:
    """/run/user/<uid> and the /dev/kvm ACL both arrive after the boot hook."""
    waited: list[int] = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: waited.append(s))
    monkeypatch.setattr(lc.os.path, "isdir", lambda p: True)  # runtime dir exists
    monkeypatch.setattr(lc.Path, "exists", lambda self: False)  # no /dev/kvm on this host

    assert lc.wait_until_podman_can_run(timeout=0) == []
    assert waited == []


def test_restore_reports_failure_so_the_hook_can_retry(monkeypatch) -> None:
    """`restore` exiting 0 when every VM failed is what made a broken boot
    indistinguishable from a working one."""
    from types import SimpleNamespace

    from vmhub import cli, lifecycle

    seen: list[list[str]] = []

    def fake_restore(progress=None, failures=None):
        assert failures is not None, "cmd_restore must pass a failure sink"
        seen.append(failures)
        failures.append("winni")
        return []

    monkeypatch.setattr(lifecycle, "restore_run_state", fake_restore)
    out = SimpleNamespace(step=lambda *_a, **_k: None, say=lambda *_a, **_k: None)
    args = SimpleNamespace(quiet=True)

    assert cli.cmd_restore(args, out) == 1
    assert seen == [["winni"]]

    monkeypatch.setattr(
        lifecycle, "restore_run_state", lambda progress=None, failures=None: []
    )
    assert cli.cmd_restore(args, out) == 0
