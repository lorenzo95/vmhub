from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
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
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
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


def open_url(url: str) -> None:
    if os.environ.get("VMHUB_NO_LAUNCH"):
        return
    launcher = shutil.which("xdg-open")
    if launcher:
        Gtk.UriLauncher.new(url).launch(None, None, None)
    else:
        subprocess.Popen(["xdg-open", url])


def filter_store(name: str, patterns: list[str]) -> Gio.ListStore:
    store = Gio.ListStore.new(Gtk.FileFilter)
    flt = Gtk.FileFilter()
    flt.set_name(name)
    for pattern in patterns:
        flt.add_pattern(pattern)
    store.append(flt)
    return store


def confirm(parent: Gtk.Window, heading: str, body: str, *, destructive: bool = True) -> Callable[[], None] | None:
    dialog = Gtk.AlertDialog(
        message=heading,
        detail=body,
        buttons=["Cancel", "Confirm"],
        cancel_button=0,
        default_button=1,
    )

    def on_choice(_dialog, result) -> None:
        try:
            choice = dialog.choose_finish(result)
        except Exception:
            return
        if choice == 1:
            run_task(parent, lambda _p: None, lambda _r: None)

    dialog.choose(parent, None, on_choice)
    return None


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
    ) -> None:
        super().__init__(modal=True, transient_for=parent, title=heading)
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self.on_accept = on_accept

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

    def _on_accept_clicked(self, _widget: object) -> None:
        text = self.entry.get_text().strip()
        callback = self.on_accept
        self.close()
        if callback is not None and text:
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
) -> TextPrompt:
    window = TextPrompt(
        parent,
        heading,
        body,
        default=default,
        placeholder=placeholder,
        accept_label=accept_label,
        extra=extra,
        on_accept=on_accept,
    )
    window.present()
    return window


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
