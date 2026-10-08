"""The boot hook has to work from a directory it did not choose.

`su -` gives a login shell in the VM owner's home. project_root() only walks
upward from the cwd, so from ~/ it can never reach ~/vmhub, which is below
it, and its single fallback is hardcoded to ~/vms. The hook therefore failed
on every boot -- and because each branch ended in `|| true` followed by
`exit 0`, it failed *successfully*, which is why it took a reboot and a
report to find.
"""

from __future__ import annotations

from vmhub import autostart


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
