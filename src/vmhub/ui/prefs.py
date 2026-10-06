"""The app-wide defaults, editable.

A VM's vm.toml says what is true of that machine. These say what you want
every machine to do by default: the account you sign in to Windows with,
and the folder you want mapped into every RDP session. They live outside
the checkout so they survive a re-clone, and any VM that sets its own
value wins over what is here.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import settings  # noqa: E402
from . import widgets  # noqa: E402


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
        for key, description in settings.FIELDS.items():
            outer.append(widgets.section(key.replace("_", " ").capitalize()))
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            entry.set_text(current.get(key, ""))
            entry.set_placeholder_text("not set")
            entry.set_tooltip_text(description)
            self.entries[key] = entry
            outer.append(widgets.field(description, entry))

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

    def _on_save(self, _button: object = None) -> None:
        values = {key: entry.get_text().strip() for key, entry in self.entries.items()}
        # Bound before the call, not by it: `path` was assigned only by the
        # very call that raises, so the error handler below raised
        # UnboundLocalError instead of reporting the failure it exists to
        # report. config_path() is the same file settings.save() writes.
        target = settings.config_path()
        try:
            path = settings.save(values)
        except OSError as exc:
            self.status.set_text(f"Could not write {target}: {exc}")
            return
        self.status.set_text(f"Saved to {path}")
        self.close()