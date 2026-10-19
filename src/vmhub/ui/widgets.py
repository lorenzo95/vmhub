from __future__ import annotations

import os
import shutil
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import blueprints, lifecycle, paths, registry, spec  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import style, workers  # noqa: E402


class StateDot(Gtk.DrawingArea):
    def __init__(self, state: str = "unknown") -> None:
        super().__init__()
        self._state = state
        self.set_size_request(12, 12)
        self.add_css_class("dot")
        self.set_tooltip_text(state)
        self.set_draw_func(self._draw)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.set_tooltip_text(state)
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> bool:
        r, g, b = _rgb(style.state_colour(self._state))
        cr.set_source_rgb(r, g, b)
        cr.arc(width / 2, height / 2, min(width, height) / 2 - 1, 0, 2 * 3.14159)
        cr.fill()
        return True


class VmRow(Gtk.Box):
    def __init__(self, status) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("vmrow")
        self.dot = StateDot(status.summary)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        column.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.name_label = Gtk.Label()
        self.name_label.add_css_class("vmname")
        title_row.append(self.name_label)
        self.badge_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        title_row.append(self.badge_row)
        column.append(title_row)

        self.meta_label = Gtk.Label()
        self.meta_label.add_css_class("vmmeta")
        self.meta_label.set_xalign(0.0)
        column.append(self.meta_label)

        self.append(self.dot)
        self.append(column)
        self._badge_signature: tuple[str, ...] = ()
        self.update(status)

    def update(self, status) -> None:
        self.dot.set_state(status.summary)
        self.name_label.set_text(status.name)
        self.name_label.set_tooltip_text(status.name)

        bits = [status.summary, f"{status.cpus} cpu", status.ram]
        if status.disk_actual:
            bits.append(status.disk_actual)
        if status.template_source:
            bits.append(f"of {status.template_source}")
        self.meta_label.set_text(" · ".join(bits))

        tags: list[tuple[str, str]] = []
        if status.is_template:
            tags.append(("template", "template"))
        if status.template_source:
            tags.append(("clone", "clone"))
        if status.snapshots:
            tags.append((str(len(status.snapshots)), "snap"))
        signature = tuple(text for text, _ in tags)
        if signature != self._badge_signature:
            self._badge_signature = signature
            child = self.badge_row.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.badge_row.remove(child)
                child = nxt
            for text, kind in tags:
                self.badge_row.append(badge(text, kind))
            self.badge_row.set_visible(bool(tags))


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    value = hex_colour.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def badge(text: str, kind: str) -> Gtk.Widget:
    label = Gtk.Label(label=text)
    label.add_css_class("badge")
    label.add_css_class(kind)
    return label


def key_value_grid(pairs: list[tuple[str, str]]) -> Gtk.Widget:
    grid = Gtk.Grid(column_spacing=14, row_spacing=6)
    grid.add_css_class("kv")
    for row, (key, value) in enumerate(pairs):
        key_label = Gtk.Label(label=key)
        key_label.add_css_class("kvkey")
        key_label.set_xalign(0.0)
        key_label.set_size_request(150, -1)
        value_label = Gtk.Label(label=value)
        value_label.set_xalign(0.0)
        value_label.set_wrap(True)
        value_label.set_selectable(True)
        grid.attach(key_label, 0, row, 1, 1)
        grid.attach(value_label, 1, row, 1, 1)
    return grid


def section(title: str) -> Gtk.Label:
    label = Gtk.Label(label=title)
    label.add_css_class("section")
    label.set_xalign(0.0)
    return label


def field(label: str, widget: Gtk.Widget, hint: str = "") -> Gtk.Widget:
    """A labelled editable control, laid out as a narrow column of rows.

    key_value_grid is for facts that cannot be edited; this is for the ones
    that can, which is why it takes the control itself rather than a string.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(4)
    caption = Gtk.Label(label=label)
    caption.add_css_class("hint")
    caption.set_xalign(0.0)
    box.append(caption)
    box.append(widget)
    if hint:
        note = Gtk.Label(label=hint)
        note.add_css_class("hint")
        note.set_wrap(True)
        note.set_xalign(0.0)
        box.append(note)
    return box


def copy_row(text: str, label: str = "Copy", *, tooltip: str = "") -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    entry = Gtk.Label(label=text)
    entry.set_xalign(0.0)
    entry.set_wrap(True)
    entry.set_selectable(True)
    entry.set_focusable(True)
    entry.add_css_class("mono")

    button = Gtk.Button(label=label)
    button.add_css_class("suggested-action")
    button.set_halign(Gtk.Align.START)
    full = tooltip or text
    entry.set_tooltip_text(full)
    button.set_tooltip_text(full)

    def on_copy(_button) -> None:
        # Copy what the row says now, not the string this button was built
        # with. set_copy_text() replaces the text as the VM's state changes,
        # and a button still holding the original always copied "" -- which
        # is every Copy button on the Console tab, since they are all
        # created empty and filled in afterwards.
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(entry.get_text())
        button.set_label("Copied")
        GLib.timeout_add_seconds(2, lambda: (button.set_label(label), False)[1])

    button.connect("clicked", on_copy)
    box.append(entry)
    box.append(button)
    return box


def set_copy_text(row: Gtk.Widget, text: str) -> None:
    child = row.get_first_child()
    if isinstance(child, Gtk.Label):
        child.set_text(text)
        child.set_tooltip_text(text)


def scroll(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    window = Gtk.ScrolledWindow()
    window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    window.set_child(child)
    window.set_vexpand(True)
    return window


def open_url(url: str, *, report: Callable[[str], None] | None = None) -> None:
    """Hand a URL to the desktop, and say so when that fails.

    `report` receives the reason. It is a callback and not a return
    value because Gtk.UriLauncher.launch() is asynchronous: the
    failure arrives after this function has already returned, so
    there is nothing to return it in.

    Both of these used to be silent. launch() was called with three
    Nones, which passes no callback, so the GError was thrown away
    and a launch that failed raised nothing and showed nothing. And
    the else branch -- which ran only when xdg-open was absent, and
    then executed xdg-open -- could only ever raise FileNotFound
    Error. There was no case where it worked.
    """
    if os.environ.get("VMHUB_NO_LAUNCH"):
        return
    say = report if report is not None else (lambda _message: None)

    if shutil.which("xdg-open") is None and shutil.which("gio") is None:
        # Nothing on this machine can hand a URL to a browser, so
        # say what to do about it rather than failing on launch().
        say(
            f"Cannot open a browser: neither xdg-open nor gio is "
            f"installed. Open this by hand: {url}"
        )
        return

    def launched(launcher, result, _url: str = url) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            # The commonest cause is no handler registered for the
            # scheme, which is why the URL is repeated here: the user
            # still has to reach it somehow.
            say(f"Could not open the console ({exc.message}). Open by hand: {_url}")

    Gtk.UriLauncher.new(url).launch(None, None, launched)


def filter_store(name: str, patterns: list[str]) -> Gio.ListStore:
    store = Gio.ListStore.new(Gtk.FileFilter)
    flt = Gtk.FileFilter()
    flt.set_name(name)
    for pattern in patterns:
        flt.add_pattern(pattern)
    store.append(flt)
    return store


class TextPrompt(Gtk.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        heading: str,
        body: str,
        *,
        default: str = "",
        placeholder: str = "",
        accept_label: str = "OK",
        extra: Gtk.Widget | None = None,
        on_accept: Callable[[str], None] | None = None,
        show_entry: bool | None = None,
    ) -> None:
        super().__init__(modal=True, transient_for=parent, title=heading)
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self.on_accept = on_accept
        # A caller that brings its own fields has no use for the built-in
        # entry, and leaving it visible invites the user to type into a box
        # that nothing reads. The accept rule follows: an empty entry means
        # "not filled in" only when the entry is the input.
        self.entry_is_input = (extra is None) if show_entry is None else show_entry

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(16)
        root.set_margin_end(16)

        title = Gtk.Label(label=heading)
        title.add_css_class("title-4")
        title.set_xalign(0.0)
        title.set_wrap(True)
        root.append(title)

        if body:
            detail = Gtk.Label(label=body)
            detail.add_css_class("dim")
            detail.set_xalign(0.0)
            detail.set_wrap(True)
            root.append(detail)

        if extra is not None:
            root.append(extra)

        self.entry: Gtk.Entry | None = None
        if self.entry_is_input:
            self.entry = Gtk.Entry(text=default)
            if placeholder:
                self.entry.set_placeholder_text(placeholder)
            self.entry.connect("activate", self._on_accept_clicked)
            root.append(self.entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.accept_button = Gtk.Button(label=accept_label)
        self.accept_button.add_css_class("suggested-action")
        self.accept_button.connect("clicked", self._on_accept_clicked)
        buttons.append(cancel)
        buttons.append(self.accept_button)
        root.append(buttons)

        self.set_child(root)
        # A prompt exists only to be shown. Presenting here rather than leaving it
        # to the caller removes the failure mode where a dialog is constructed,
        # never displayed, and silently does nothing.
        self.present()

    def _on_accept_clicked(self, _widget: object) -> None:
        text = self.entry.get_text().strip() if self.entry is not None else ""
        callback = self.on_accept
        self.close()
        if callback is None:
            return
        if text or not self.entry_is_input:
            callback(text)


def prompt_text(
    parent: Gtk.Window,
    heading: str,
    body: str,
    default: str = "",
    on_accept: Callable[[str], None] | None = None,
    *,
    placeholder: str = "",
    accept_label: str = "OK",
    extra: Gtk.Widget | None = None,
    show_entry: bool | None = None,
) -> TextPrompt:
    return TextPrompt(
        parent,
        heading,
        body,
        default=default,
        placeholder=placeholder,
        accept_label=accept_label,
        extra=extra,
        on_accept=on_accept,
        show_entry=show_entry,
    )


def report_error(parent: Gtk.Window, exc: BaseException) -> None:
    dialog = Gtk.AlertDialog(
        message="Operation failed",
        detail=workers.describe(exc),
        buttons=["Close"],
        default_button=0,
        cancel_button=0,
    )
    dialog.show(parent)


def run_task(
    parent: Gtk.Window,
    work,
    on_done=None,
    *,
    label: str = "",
    on_error=None,
) -> None:
    status = getattr(parent, "_vmhub_status", None)
    if status is not None and label:
        status.push(label)

    def handle_error(exc: BaseException) -> bool:
        if status is not None:
            status.pop()
        report_error(parent, exc)
        return False

    def handle_done(result) -> bool:
        if status is not None:
            status.pop()
        if on_done is not None:
            on_done(result)
        refresh = getattr(parent, "refresh", None)
        if callable(refresh):
            refresh()
        return False

    workers.run_async(
        work,
        on_progress=(lambda m: status.push(m, transient=True)) if status is not None else None,
        on_done=handle_done,
        on_error=handle_error if on_error is None else on_error,
    )


class NamePromptDialog(Gtk.Window):
    """Modal single-field dialog: ask the user for a VM name.

    Used to resolve import name collisions. The dialog fires ``on_response`` with
    the entered name (or ``None`` if cancelled) and closes; it does not block.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        title: str,
        message: str,
        default: str = "",
        confirm_label: str = "Use this name",
    ) -> None:
        super().__init__(modal=True, transient_for=parent, title=title)
        self._response: Callable[[str | None], None] | None = None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)

        heading = Gtk.Label(label=message)
        heading.set_xalign(0.0)
        heading.set_wrap(True)
        root.append(heading)

        self._entry = Gtk.Entry(text=default)
        if default:
            # Highlight the suggestion so a tap on Enter keeps it
            self._entry.select_region(0, len(default))
        self._entry.connect("activate", lambda *_a: self._emit_ok())
        root.append(self._entry)

        # spec.validate_name runs on every keystroke so the OK button tracks the
        # same rule the real importer does -- "starts with a letter, then letters,
        # digits, dash or underscore". Without this the dialog can hand a bad
        # name back to the import, which then raises a different exception.
        self._error_label = Gtk.Label()
        self._error_label.add_css_class("errrow")
        self._error_label.set_xalign(0.0)
        self._error_label.set_wrap(True)
        root.append(self._error_label)

        self._ok_button = Gtk.Button(label=confirm_label)
        self._ok_button.add_css_class("suggested-action")
        self._ok_button.connect("clicked", lambda *_a: self._emit_ok())

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda *_a: self._emit_cancel())

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.append(cancel_button)
        buttons.append(self._ok_button)
        root.append(buttons)

        self.set_child(root)
        self.set_default_size(420, -1)
        self._entry.connect("changed", lambda *_a: self._refresh())
        self._refresh()

    def open(self, on_response: Callable[[str | None], None]) -> None:
        self._response = on_response
        self.present()
        self._entry.grab_focus()

    def _refresh(self) -> None:
        name = self._entry.get_text().strip()
        if not name:
            self._error_label.set_text("Name cannot be empty.")
            self._ok_button.set_sensitive(False)
            return
        try:
            spec.validate_name(name)
        except VmhubError as exc:
            self._error_label.set_text(str(exc))
            self._ok_button.set_sensitive(False)
            return
        self._error_label.set_text("")
        self._ok_button.set_sensitive(True)

    def _emit_ok(self) -> None:
        if not self._ok_button.get_sensitive():
            return
        cb, self._response = self._response, None
        if cb is not None:
            cb(self._entry.get_text().strip())
        self.destroy()

    def _emit_cancel(self) -> None:
        cb, self._response = self._response, None
        if cb is not None:
            cb(None)
        self.destroy()
