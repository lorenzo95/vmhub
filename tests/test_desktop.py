"""The start-menu entry, and the fact that installing it is reversible.

The entry is a small generated file, but two things about it are easy to
get wrong in ways that are invisible until the menu is opened.

First, it names the checkout by absolute path. That is deliberate -- moving
or deleting the checkout is what should make the entry stop working --
but it also means the Exec line is only right for the machine it was
generated on.

Second, and more dangerous to test casually: the file goes into the VM
owner's real ~/.local/share/applications. A test that installs one for
real puts an entry in the menu of whoever is running the suite, so every
test here redirects desktop._owner_home at a temp directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vmhub import desktop
from vmhub.errors import LifecycleError


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the entry out of the real application menu.

    The sandbox project root ships a stub vmctl but no GUI launcher, and
    install() refuses to write an entry that launches nothing -- so give it
    one. It only has to exist: desktop.py never runs it.
    """
    from vmhub import paths

    (paths.project_root() / "vmhub").write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(desktop, "_owner_home", lambda: home)
    return home


def test_install_writes_the_entry(fake_home: Path) -> None:
    lines = desktop.install()
    assert lines, "install said nothing"
    target = fake_home / ".local" / "share" / "applications" / "vmhub.desktop"
    assert target.is_file(), target
    assert "Type=Application" in target.read_text()


def test_the_entry_passes_desktop_file_validate(fake_home: Path) -> None:
    """A generated entry the menu silently ignores looks exactly like one
    that was never installed."""
    if not shutil.which("desktop-file-validate"):
        pytest.skip("desktop-file-validate not installed")
    desktop.install()
    target = desktop.desktop_path()
    assert desktop._validate(target.read_text()) is None


def test_exec_points_at_this_checkout(fake_home: Path) -> None:
    """Not "vmhub": the menu does not run a login shell, and a bare name
    resolves against the desktop's own PATH, which is not the one the
    checkout was installed from."""
    desktop.install()
    text = desktop.desktop_path().read_text()
    exec_line = next(l for l in text.splitlines() if l.startswith("Exec="))
    target = Path(exec_line[len("Exec=") :])
    assert target.is_absolute(), exec_line
    assert target.is_file(), f"Exec names a file that is not there: {target}"


def test_tryexec_guards_a_moved_checkout(fake_home: Path) -> None:
    """TryExec is what makes the entry disappear rather than fail."""
    text = desktop.entry_text()
    assert "TryExec=" in text


def test_installing_twice_is_fine(fake_home: Path) -> None:
    desktop.install()
    desktop.install()
    assert desktop.desktop_path().is_file()


def test_uninstall_removes_it(fake_home: Path) -> None:
    desktop.install()
    lines = desktop.uninstall()
    assert lines and "removed" in lines[0], lines
    assert not desktop.desktop_path().exists()


def test_uninstall_when_absent_says_so_rather_than_failing(fake_home: Path) -> None:
    """The usual reason to uninstall is that it was never installed."""
    lines = desktop.uninstall()
    assert lines and "nothing to remove" in lines[0], lines


def test_status_follows_the_file(fake_home: Path) -> None:
    assert desktop.status()["menu_entry"] is False
    desktop.install()
    assert desktop.status()["menu_entry"] is True
    desktop.uninstall()
    assert desktop.status()["menu_entry"] is False


def test_install_refuses_when_there_is_no_gui_launcher(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better to say so than to write an entry that launches nothing."""
    monkeypatch.setattr(desktop, "gui_path", lambda: fake_home / "not-here" / "vmhub")
    with pytest.raises(LifecycleError):
        desktop.install()
    assert not desktop.desktop_path().exists()


def test_the_entry_carries_no_icon_line() -> None:
    """There is no icon in the tree, and an Icon= naming a file that is
    not there gets a broken placeholder rather than a generic icon."""
    assert "Icon=" not in desktop.entry_text()


def test_the_entry_is_not_the_session_autostart_file(fake_home: Path) -> None:
    """Different directories, different jobs: this one puts vmhub in the
    menu, autostart starts it at login."""
    desktop.install()
    here = desktop.desktop_path()
    autostart_entry = fake_home / ".config" / "autostart" / "vmhub.desktop"
    assert here != autostart_entry
    assert not autostart_entry.exists()