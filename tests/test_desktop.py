"""The start-menu entry, and the fact that installing it is reversible.

The entry is a small generated file, but three things about it are easy to
get wrong in ways that are invisible until the menu is opened.

First, it names the checkout by absolute path. That is deliberate -- moving
or deleting the checkout is what should make the entry stop working --
but it also means the Exec line is only right for the machine it was
generated on.

Second, the icon is a *name*, not a path, and it only resolves because the
SVG next to this entry got copied into the user's hicolor directory. Point
Icon= at a file that is not there and the menu draws a broken placeholder,
which looks like a bug in the menu rather than in us.

Third, and most dangerous to test casually: the file goes into the VM
owner's real ~/.local/share/applications. A test that installs one for
real puts an entry in the menu of whoever is running the suite, so every
test here redirects desktop._owner_home at a temp directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree

import pytest

from vmhub import desktop
from vmhub.errors import LifecycleError


def _shipped_icon() -> Path:
    """The icon that actually ships, not the sandbox placeholder.

    fake_home writes a one-line stub into the sandbox project root, and
    asserting things about that would prove nothing about the artwork that
    gets installed. desktop.__file__ is the real module, so the icons
    directory beside it is the real one.
    """
    return Path(desktop.__file__).parent / "icons" / f"{desktop.ICON_NAME}.svg"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the entry out of the real application menu.

    The sandbox project root ships a stub vmctl but no GUI launcher and no
    icon, and install() refuses to write an entry that launches nothing or
    shows nothing -- so give it both. Neither is ever run or rendered.
    """
    from vmhub import paths

    root = paths.project_root()
    (root / "vmhub").write_text("#!/bin/sh\n")
    icon = root / desktop.ICON_SOURCE_RELATIVE
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n")
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


def test_the_entry_runs_in_the_checkout(fake_home: Path) -> None:
    """Path=, not decoration -- see the comment in desktop.entry_text().

    An absolute Exec line still gets run in whatever directory the desktop
    picks, which for a menu entry is the user's home. This is the key that
    stops the GUI coming up finding no VMs.
    """
    from vmhub import paths

    text = desktop.entry_text()
    path_line = next(l for l in text.splitlines() if l.startswith("Path="))
    assert path_line == f"Path={paths.project_root()}", path_line
    assert Path(path_line[len("Path=") :]).is_dir(), path_line


def test_the_directory_it_pins_is_one_the_project_actually_lives_in(
    fake_home: Path,
) -> None:
    """Path= has to agree with Exec=, or the entry runs the right program in
    the wrong checkout -- which on a machine with two clones shows the VMs of
    one while the launcher belongs to the other."""
    from vmhub import paths

    lines = desktop.entry_text().splitlines()
    exec_target = Path(
        next(l for l in lines if l.startswith("Exec="))[len("Exec=") :]
    )
    path_target = Path(
        next(l for l in lines if l.startswith("Path="))[len("Path=") :]
    )
    assert exec_target.parent == path_target, (exec_target, path_target)
    assert exec_target.parent == paths.project_root()


def test_the_installed_entry_still_carries_the_working_directory(
    fake_home: Path,
) -> None:
    """Same reason the icon has a test that reads the file rather than
    entry_text(): what the menu gets is the text on disk."""
    desktop.install()
    text = desktop.desktop_path().read_text()
    assert "\nPath=" in text, text


def test_the_working_directory_is_load_bearing(
    project_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason Path= exists at all: from anywhere else, discovery fails.

    Pinned so the key cannot be dropped as cruft -- it looks redundant next
    to an absolute Exec, and the only symptom of removing it is a GUI that
    lists no VMs.
    """
    from vmhub import paths
    from vmhub.errors import ProjectRootError

    # The entry names the directory that does work...
    assert f"Path={project_home}" in desktop.entry_text()

    # ...and from anywhere else, discovery fails. monkeypatch.context, not
    # undo(): undo would also drop the sandbox's VMHUB_HOME and leave the
    # rest of the test running against the real checkout.
    elsewhere = tmp_path / "not-the-checkout"
    elsewhere.mkdir()
    with monkeypatch.context() as patch:
        patch.delenv(paths.ENV_HOME, raising=False)
        patch.chdir(elsewhere)
        with pytest.raises(ProjectRootError):
            paths.project_root()


def test_the_entry_names_the_window_it_is_meant_to_bind_to(
    fake_home: Path,
) -> None:
    """StartupWMClass is how a running window is matched to this entry.

    Without it the match has to happen by luck, on WM_CLASS and the entry's
    filename agreeing, and nothing in either file says that they are supposed
    to.
    """
    desktop.install()
    text = desktop.desktop_path().read_text()
    line = next(l for l in text.splitlines() if l.startswith("StartupWMClass="))
    assert line == f"StartupWMClass={desktop.WM_CLASS}", line


def test_the_entry_it_writes_is_valid_with_the_window_binding_in_it(
    fake_home: Path,
) -> None:
    """Adding a key to a generated file is exactly how an entry starts being
    silently ignored by the menu."""
    if not shutil.which("desktop-file-validate"):
        pytest.skip("desktop-file-validate not installed")
    desktop.install()
    assert desktop._validate(desktop.desktop_path().read_text()) is None


def test_the_window_class_agrees_with_the_name_the_gui_claims() -> None:
    """The binding is only real if both halves hold the same string.

    Skipped rather than failed when GTK is absent: vmctl has to stay usable,
    and testable, on a machine with only the CLI packages installed.
    """
    pytest.importorskip("gi", reason="PyGObject/GTK not installed")
    from vmhub.ui.app import PRGNAME

    assert PRGNAME == desktop.WM_CLASS, (
        "the window calls itself one thing and the entry names another, so "
        "the dock has no .desktop to match the window against"
    )


def test_building_the_app_claims_that_name_for_real() -> None:
    """The behaviour itself, not a string in the source.

    Constructing a Gtk.Application is safe without a display -- it does not
    register or open anything -- so this runs in the plain test suite rather
    than only under the xvfb run of ./guicheck.
    """
    pytest.importorskip("gi", reason="PyGObject/GTK not installed")
    from gi.repository import GLib

    from vmhub.ui.app import VmhubApp

    GLib.set_prgname("not-vmhub")
    VmhubApp()
    assert GLib.get_prgname() == desktop.WM_CLASS, (
        "the window keeps the module filename as its WM_CLASS, so no dock can "
        "find vmhub.desktop for it"
    )


def test_the_launchers_name_the_checkout_rather_than_leaving_it_to_the_cwd(
) -> None:
    """Defense in depth behind Path=, and the fix for every other launcher.

    A desktop that ignores Path= -- or a user who runs the GUI from a file
    manager, or a session autostart -- gets the same project root anyway,
    because the launcher exports it from the directory it lives in. Same
    reasoning as the systemd unit's Environment= and the sysvinit hook's cd.
    """
    root = Path(desktop.__file__).resolve().parent.parent.parent
    for script in ("vmhub", "vmctl"):
        text = (root / script).read_text(encoding="utf-8")
        # The ${VMHUB_HOME:-...} form, not VMHUB_HOME="$HERE". A desktop
        # session has no VMHUB_HOME and must land on this checkout, which is
        # what fixes the menu entry; an explicit one must still win, because
        # the README documents it as the way to point vmhub at another
        # checkout. Hardcoding it made that documented override a no-op.
        assert (
            'export VMHUB_HOME="${VMHUB_HOME:-$HERE}"' in text
        ), (
            f"{script} must default the project root to its own directory "
            "while still honouring an explicit VMHUB_HOME"
        )
        assert (
            'export VMHUB_HOME="$HERE"' not in text
        ), (
            f"{script} overrides VMHUB_HOME unconditionally, so pointing "
            "vmhub at another checkout silently does nothing"
        )


def test_the_entry_names_the_icon_rather_than_pathing_to_it() -> None:
    """A theme name is what lets the desktop substitute and, failing that,
    fall back. A path to the installed copy is correct for exactly one
    machine and is a broken image on every other one."""
    icon_line = next(
        l for l in desktop.entry_text().splitlines() if l.startswith("Icon=")
    )
    assert icon_line == f"Icon={desktop.ICON_NAME}", icon_line
    assert "/" not in icon_line, icon_line


def test_install_copies_the_icon_where_the_menu_looks_for_it(
    fake_home: Path,
) -> None:
    """hicolor is the fallback theme every desktop searches, so an icon
    there is found whatever theme is actually set."""
    desktop.install()
    installed = fake_home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "vmhub.svg"
    assert installed.is_file(), installed
    from vmhub import paths

    source = paths.project_root() / desktop.ICON_SOURCE_RELATIVE
    assert installed.read_bytes() == source.read_bytes()


def test_the_installed_icon_matches_the_name_in_the_entry(
    fake_home: Path,
) -> None:
    """The entry says Icon=vmhub, so the file has to be called vmhub.svg in
    the standard scalable/apps layout, not anything else in that tree."""
    desktop.install()
    icon_line = next(
        l for l in desktop.desktop_path().read_text().splitlines()
        if l.startswith("Icon=")
    )
    name = icon_line.split("=", 1)[1]
    assert desktop.icon_path() == desktop.icon_dir() / f"{name}.svg"
    assert desktop.icon_path().is_file()


def test_install_asks_the_icon_theme_to_index_what_it_just_wrote(
    fake_home: Path,
) -> None:
    """A copied icon that nothing has indexed is one the menu cannot find,
    and the failure is invisible: the entry just shows the wrong picture."""
    if not shutil.which("gtk-update-icon-cache"):
        pytest.skip("gtk-update-icon-cache not installed")
    desktop.install()
    cache = desktop.icon_theme_dir() / "icon-theme.cache"
    assert cache.is_file(), f"no icon cache at {cache}"


def test_install_refuses_when_the_checkout_has_no_icon(
    fake_home: Path,
) -> None:
    """The broken-placeholder case. Better to say so and write nothing than
    to write an entry pointing at an icon that is not there."""
    from vmhub import paths

    (paths.project_root() / desktop.ICON_SOURCE_RELATIVE).unlink()
    with pytest.raises(LifecycleError):
        desktop.install()
    assert not desktop.desktop_path().exists()
    assert not desktop.icon_path().exists()


def test_installing_twice_is_fine(fake_home: Path) -> None:
    desktop.install()
    desktop.install()
    assert desktop.desktop_path().is_file()
    assert desktop.icon_path().is_file()


def test_uninstall_removes_it(fake_home: Path) -> None:
    desktop.install()
    lines = desktop.uninstall()
    assert lines and "removed" in lines[0], lines
    assert not desktop.desktop_path().exists()


def test_uninstall_takes_the_icon_with_it(fake_home: Path) -> None:
    """Leaving the icon behind is litter: it is ours, we put it there, and
    nothing else claims it."""
    desktop.install()
    assert desktop.icon_path().is_file()
    desktop.uninstall()
    assert not desktop.icon_path().exists()


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


def test_status_reports_the_icon_separately(fake_home: Path) -> None:
    """A half-installed state -- entry present, icon gone -- is exactly the
    one where `status` is the only place the problem is visible."""
    assert desktop.status()["icon_installed"] is False
    desktop.install()
    assert desktop.status()["icon_installed"] is True
    desktop.icon_path().unlink()
    assert desktop.status()["menu_entry"] is True
    assert desktop.status()["icon_installed"] is False


def test_install_refuses_when_there_is_no_gui_launcher(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better to say so than to write an entry that launches nothing."""
    monkeypatch.setattr(desktop, "gui_path", lambda: fake_home / "not-here" / "vmhub")
    with pytest.raises(LifecycleError):
        desktop.install()
    assert not desktop.desktop_path().exists()


def test_the_entry_is_not_the_session_autostart_file(fake_home: Path) -> None:
    """Different directories, different jobs: this one puts vmhub in the
    menu, autostart starts it at login."""
    desktop.install()
    here = desktop.desktop_path()
    autostart_entry = fake_home / ".config" / "autostart" / "vmhub.desktop"
    assert here != autostart_entry
    assert not autostart_entry.exists()


def test_the_icon_ships_where_install_looks_for_it() -> None:
    """If this file moves, install() either copies nothing or copies the
    wrong thing, and neither shows up until the menu is opened.

    The checkout and the install destination are different directories on
    purpose: the repo holds the artwork, ~/.local/share/icons holds the copy.
    """
    icon = _shipped_icon()
    assert icon.is_file(), icon
    assert icon.name == f"{desktop.ICON_NAME}.svg"
    assert icon.parent.name == "icons", icon
    assert icon.parent.parent == Path(desktop.__file__).parent, icon
    # ...and the destination is the layout an icon theme actually searches.
    assert desktop.icon_path().parts[-4:] == (
        "hicolor",
        "scalable",
        "apps",
        f"{desktop.ICON_NAME}.svg",
    )


def test_the_shipped_icon_is_well_formed_xml() -> None:
    """A malformed icon is not an image. Nothing in the chain explains why:
    rsvg, GTK and the menu each just decline to draw it, and install()
    reports success throughout."""
    root = ElementTree.parse(_shipped_icon()).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg", root.tag


def test_the_shipped_icon_opens_with_its_root_element() -> None:
    """gdk-pixbuf works out what a file is from the head of it, and it
    refused an earlier draft of this icon outright: the comment sat in front
    of <svg and the whole file came back as "unrecognized image file format".
    The menu renders that as a broken placeholder while install() reports a
    successful install, so nothing anywhere names a cause.

    Which byte exactly makes it give up is not worth pinning down. With an
    XML prolog the same comment was rejected and without one it was accepted,
    and a padded comment stopped being recognised somewhere past 200 bytes.
    The one thing that held in every combination tried is that the file has
    to start with the root element, which is what this asserts.
    """
    head = _shipped_icon().read_bytes()[:256]
    assert head.find(b"<svg") != -1, (
        "the icon does not open with its root element, so gdk-pixbuf may "
        "not recognise the file as an SVG at all"
    )


def test_the_shipped_icon_actually_draws_something() -> None:
    text = _shipped_icon().read_text(encoding="utf-8")
    assert "viewBox" in text, "a scalable icon with no viewBox scales to nothing"
    assert text.count("<rect") >= 4, "that is not much of an icon"


def test_the_icon_is_not_installed_into_a_system_icon_theme(
    fake_home: Path,
) -> None:
    """Everything this command writes belongs to one user and is undone by
    uninstall(). A file under /usr/share/icons would need sudo to remove and
    would outlive the checkout."""
    desktop.install()
    written = list((fake_home / ".local" / "share").rglob("*"))
    assert written, "install wrote nothing at all"
    assert all(str(p).startswith(str(fake_home)) for p in written)