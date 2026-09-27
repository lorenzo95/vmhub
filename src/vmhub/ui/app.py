from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from ..errors import VmhubError  # noqa: E402
from . import style  # noqa: E402
from .window import VmhubWindow  # noqa: E402


APP_ID = "dev.vmhub.GUI"


class VmhubApp(Gtk.Application):
    def __init__(self, application_id: str | None = None) -> None:
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
