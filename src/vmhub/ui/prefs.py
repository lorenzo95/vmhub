"""The app-wide defaults, editable.

A VM's vm.toml says what is true of that machine. These say what you want
every machine to do by default: the account you sign in to Windows with,
and the folder you want mapped into every RDP session. They live outside
the checkout so they survive a re-clone, and any VM that sets its own
value wins over what is here.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import settings  # noqa: E402
from . import widgets  # noqa: E402


# Fields that name a directory, and so get a chooser beside the text box.
# A path is the one kind of value here that cannot be typed out reliably:
# it has to exist, and spelling it by hand is how a trailing space or a
# "~" that is not expanded ends up saved as if it were a real folder.
FOLDER_FIELDS = {"share_path"}


def _is_dismissed(exc: GLib.Error) -> bool:
    """Did the user close the chooser without choosing?

    Both codes are checked because which one a cancelled dialog raises has
    moved between GTK releases, and the only thing that matters here is that
    cancelling is silent.
    """
    return exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED) or (
        exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.CANCELLED)
    )


class PreferencesDialog(Gtk.Window):
    """Edit settings.FIELDS, and say where the file is."""

    def __init__(self, parent: Gtk.Window | None = None) -> None:
        super().__init__(transient_for=parent, modal=True)
        self.set_title("vmhub preferences")
        self.set_default_size(560, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)

        intro = Gtk.Label(
            label=(
                "These defaults apply to every VM. A VM that sets its own value "
                "keeps it, so changing something here never overrides a machine "
                "that has already decided."
            )
        )
        intro.add_css_class("hint")
        intro.set_wrap(True)
        intro.set_xalign(0.0)
        outer.append(intro)

        current = settings.load()
        self.entries: dict[str, Gtk.Entry] = {}
        self.folder_buttons: dict[str, Gtk.Button] = {}
        for key, description in settings.FIELDS.items():
            outer.append(widgets.section(key.replace("_", " ").capitalize()))
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            entry.set_text(current.get(key, ""))
            # An empty field here means "fall back", not "off". Saying only
            # "not set" left the person looking at a blank box with no way to
            # tell what a new VM would actually get; for a field with a real
            # default, name it.
            fallback = settings.fallback(key)
            entry.set_placeholder_text(
                f"not set — defaults to {fallback}" if fallback else "not set"
            )
            entry.set_tooltip_text(description)
            self.entries[key] = entry
            control: Gtk.Widget = entry
            if key in FOLDER_FIELDS:
                control = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                control.append(entry)
                browse = Gtk.Button(label="Select folder…")
                browse.set_tooltip_text(f"Browse for the {description.lower()}")
                browse.connect("clicked", self._on_browse, key)
                self.folder_buttons[key] = browse
                control.append(browse)
            outer.append(widgets.field(description, control))

        where = Gtk.Label(label=f"Saved to {settings.config_path()}")
        where.add_css_class("hint")
        where.set_wrap(True)
        where.set_xalign(0.0)
        where.set_margin_top(6)
        outer.append(where)

        self.status = Gtk.Label()
        self.status.add_css_class("hint")
        self.status.set_xalign(0.0)
        outer.append(self.status)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.END)
        row.set_margin_top(6)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        row.append(cancel)
        row.append(save)
        outer.append(row)

        self.set_child(outer)
        # A dialog exists only to be shown. Without this the menu item was
        # wired up, not greyed out, and did nothing at all when clicked --
        # get_visible() and get_mapped() were both False. widgets.TextPrompt
        # carries this same rule and the comment explaining it; this one was
        # never given it.
        self.present()

    def _on_browse(self, _button: Gtk.Button, key: str) -> None:
        """Open the folder chooser for one of FOLDER_FIELDS.

        Async on purpose: FileDialog has no blocking call, and running a
        nested main loop to fake one is how you get a re-entrant dialog
        inside a modal that is already up.
        """
        dialog = Gtk.FileDialog()
        dialog.set_title("Select a folder")
        current = self.entries[key].get_text().strip()
        # Only start where the current value is a real directory. A value
        # that does not exist is exactly what the user is here to replace,
        # and handing that to the chooser would open on some unrelated place.
        if current and Path(current).is_dir():
            dialog.set_initial_folder(Gio.File.parse_name(current))
        dialog.select_folder(self, None, self._on_folder_chosen, key)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: object, key: str) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as exc:
            if _is_dismissed(exc):
                # Cancel is not a failure and has nothing to report; the
                # entry keeps whatever it had.
                return
            self.status.set_text(f"Could not use that folder: {exc.message}")
            return
        self._accept_folder(key, folder)

    def _accept_folder(self, key: str, folder: Gio.File) -> None:
        """Put a chosen folder in the box that gets saved.

        Split out of the callback because a native file dialog cannot be
        opened by a test, and the part worth testing is what happens to the
        answer afterwards -- it has to land in the entry Save reads.
        """
        path = folder.get_path()
        if path:
            self.entries[key].set_text(path)

    def _on_save(self, _button: object = None) -> None:
        values = {key: entry.get_text().strip() for key, entry in self.entries.items()}
        # Bound before the call, not by it: `path` was assigned only by the
        # very call that raises, so the error handler below raised
        # UnboundLocalError instead of reporting the failure it exists to
        # report. config_path() is the same file settings.save() writes.
        target = settings.config_path()
        try:
            path = settings.save(values)
        except settings.SettingError as exc:
            # The field is not a tag; say which and why, and stay open so the
            # value can be corrected rather than retyped.
            self.status.set_text(str(exc))
            return
        except OSError as exc:
            self.status.set_text(f"Could not write {target}: {exc}")
            return
        self.status.set_text(f"Saved to {path}")
        self.close()