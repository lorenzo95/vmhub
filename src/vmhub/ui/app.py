from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from ..autostart import WM_CLASS  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import style  # noqa: E402
from .window import VmhubWindow  # noqa: E402


APP_ID = "dev.vmhub.GUI"

# GTK takes WM_CLASS from GLib's program name, and `python3 -m vmhub.ui.app`
# leaves that as the basename of the module file -- the window was calling
# itself "app.py", after app.py. A dock resolves a window to an application by
# looking for <WM_CLASS>.desktop, and there is no app.py.desktop anywhere, so
# the window had no icon to group under and "Pin to Dock" had nothing stable to
# hold. WM_CLASS is what both entry templates write into StartupWMClass, so
# claiming it is what makes vmhub.desktop find the window.
PRGNAME = WM_CLASS


class VmhubApp(Gtk.Application):
    def __init__(self, application_id: str | None = None) -> None:
        GLib.set_prgname(PRGNAME)
        super().__init__(
            application_id=application_id or os.environ.get("VMHUB_APP_ID") or APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.window: VmhubWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            style.apply(self)
            self.window = VmhubWindow(self)
        self.window.present()

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *a: self.quit())
        self.add_action(action)
        new_vm = Gio.SimpleAction.new("new-vm", None)
        new_vm.connect("activate", lambda *a: self.window and self.window.new_vm())
        self.add_action(new_vm)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("app.new-vm", ["<primary>n"])
        self.set_accels_for_action("win.refresh", ["<primary>r", "F5"])


def main(argv: list[str] | None = None) -> int:
    app = VmhubApp()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
