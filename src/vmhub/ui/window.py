from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

from .. import export, lifecycle  # noqa: E402
from . import widgets, workers  # noqa: E402
from .pane import VmPane  # noqa: E402


class StatusStrip(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.label = Gtk.Label()
        self.label.set_xalign(0.0)
        self.label.set_hexpand(True)
        self.label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.label.set_tooltip_text("")
        self.label.add_css_class("dim")
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.append(self.label)
        self.append(self.spinner)
        self._active = 0
        self._history: list[str] = []

    def push(self, message: str, *, transient: bool = False) -> None:
        """Show the newest message, and keep a short scrollback on the tooltip.

        Transient messages are the incremental progress a long operation emits.
        They used to be stashed into a set and never displayed, so a clone or an
        export showed nothing at all between "starting" and "done".
        """
        self._active += 1
        self.label.set_text(message)
        self._history.append(message)
        del self._history[:-8]
        self.label.set_tooltip_text("\n".join(self._history))
        self.spinner.set_visible(True)
        self.spinner.start()

    def pop(self) -> None:
        self._active = max(0, self._active - 1)
        if self._active == 0:
            self.spinner.stop()
            self.spinner.set_visible(False)
            self._history.clear()
            self.label.set_tooltip_text("")

    def set_idle(self, text: str) -> None:
        if self._active == 0:
            self.label.set_text(text)
            self.label.set_tooltip_text("")


class VmhubWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application)
        self.set_title("vmhub")
        self.set_default_size(1180, 780)
        self.set_size_request(940, 600)

        self.panes: dict[str, VmPane] = {}
        self.rows: dict[str, Gtk.ListBoxRow] = {}
        self.order: list[str] = []
        self.current: str | None = None
        self._refresh_tag = 0
        self._syncing = False
        self._refreshing = False
        self._refresh_pending = False
        self._closing = False

        self._build()
        self.refresh()
        self._schedule_poll()
        self.connect("close-request", self._on_close)

    def _build(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.add_css_class("toolbar")
        header.set_margin_bottom(1)

        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Filter VMs")
        self.search.connect("search-changed", lambda _e: self._render_list())

        new_button = Gtk.Button(label="New VM")
        new_button.add_css_class("suggested-action")
        new_button.set_tooltip_text("Create a virtual machine (Ctrl+N)")
        new_button.connect("clicked", self._on_new)

        import_button = Gtk.Button(label="Import")
        import_button.set_tooltip_text("Import a vmhub bundle")
        import_button.connect("clicked", self._on_import)

        start_all = Gtk.Button(label="Start all")
        start_all.connect("clicked", self._on_start_all)
        stop_all = Gtk.Button(label="Stop all")
        stop_all.connect("clicked", self._on_stop_all)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Refresh", "win.refresh")
        section.append("Doctor", "win.doctor")
        section.append("Restore running VMs", "win.restore")
        menu.append_section(None, section)
        menu_button.set_menu_model(menu)
        self._install_actions()

        header.append(self.search)
        header.append(new_button)
        header.append(import_button)
        header.append(start_all)
        header.append(stop_all)
        header.append(menu_button)
        outer.append(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(290)
        paned.set_vexpand(True)

        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("vmlist")
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-selected", self._on_row_selected)
        self.list_box.connect("row-activated", self._on_row_activated)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        sidebar.append(scroller)

        self.sidebar_empty = Gtk.Label(
            label="No VMs yet.\n\nUse New VM to create one, or Import to load a bundle."
        )
        self.sidebar_empty.add_css_class("dim")
        self.sidebar_empty.set_wrap(True)
        self.sidebar_empty.set_margin_top(14)
        sidebar.append(self.sidebar_empty)
        self.sidebar = sidebar
        paned.set_start_child(sidebar)

        self.content = Gtk.Stack()
        self.content.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        placeholder_label = Gtk.Label(label="Select a VM on the left")
        placeholder_label.add_css_class("dim")
        self.content_empty = Gtk.Box()
        self.content_empty.set_halign(Gtk.Align.CENTER)
        self.content_empty.set_valign(Gtk.Align.CENTER)
        self.content_empty.append(placeholder_label)
        self.content.add_named(self.content_empty, "empty")
        paned.set_end_child(self.content)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        outer.append(paned)

        self.status_strip = StatusStrip()
        outer.append(self.status_strip)

        self.set_child(outer)
        self._vmhub_status = self.status_strip

    def _install_row_actions(self) -> None:
        for action_name in ("console", "start", "stop", "clone", "export", "delete"):
            action = Gio.SimpleAction.new(action_name, GLib.VariantType.new("s"))
            action.connect(
                "activate",
                lambda _a, param, kind=action_name: self._row_action(kind, param.get_string()),
            )
            self.add_action(action)

    def _install_actions(self) -> None:
        self._install_row_actions()
        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *a: self.refresh())
        self.add_action(refresh)
        doctor = Gio.SimpleAction.new("doctor", None)
        doctor.connect("activate", lambda *a: self._on_doctor())
        self.add_action(doctor)
        restore = Gio.SimpleAction.new("restore", None)
        restore.connect("activate", lambda *a: self._on_restore())
        self.add_action(restore)

    def _schedule_poll(self) -> None:
        self._refresh_tag = GLib.timeout_add_seconds(4, self._poll)

    def _poll(self) -> bool:
        if not self.is_visible():
            return True
        self.refresh()
        return True

    def refresh(self) -> None:
        """Collect VM state off the main thread, then render it here.

        list_vms() spawns subprocesses; doing it inline froze the UI for up to a
        second and a half every poll. A pass already in flight is never stacked —
        the newest request is remembered and run once it finishes.
        """
        if self._refreshing:
            self._refresh_pending = True
            return
        self._refreshing = True

        def done(statuses: list) -> bool:
            self._refreshing = False
            if self._closing:
                return False
            self._apply_statuses(statuses)
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh()
            return False

        def failed(exc: BaseException) -> bool:
            self._refreshing = False
            if not self._closing:
                self.status_strip.set_idle(f"error: {exc}")
            return False

        workers.run_async(
            lambda _progress: lifecycle.list_vms(), on_done=done, on_error=failed
        )

    def _prune_panes(self) -> None:
        """Drop panes whose VM is gone; they were never released before."""
        for name in [n for n in self.panes if n not in self.statuses]:
            pane = self.panes.pop(name)
            pane.close()
            child = self.content.get_child_by_name(name)
            if child is not None:
                self.content.remove(child)

    def _apply_statuses(self, statuses: list) -> None:
        """Render a collected snapshot. Main thread only."""
        self.statuses = {s.name: s for s in statuses}
        self._prune_panes()
        self._render_list()
        self.sidebar_empty.set_visible(not self.order)
        if self.current and self.current not in self.statuses:
            self.select_vm(None)
        elif self.current:
            self._update_pane(self.current)
        running = sum(1 for s in self.statuses.values() if s.powered)
        self.status_strip.set_idle(f"{running} running / {len(self.statuses)} total")

    def _matches(self, status: lifecycle.VmStatus) -> bool:
        needle = self.search.get_text().strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            [status.name, status.blueprint or "", status.template_source or ""]
        ).lower()
        return needle in haystack

    def _render_list(self) -> None:
        statuses = getattr(self, "statuses", {})
        visible = [s for s in statuses.values() if self._matches(s)]
        visible.sort(key=lambda s: (not s.powered, not s.is_template, s.name))
        names = [s.name for s in visible]

        if names != self.order:
            child = self.list_box.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.list_box.remove(child)
                child = nxt
            self.rows.clear()
            self.order = names
            if names:
                for status in visible:
                    row = Gtk.ListBoxRow()
                    row.add_css_class("vmrowhost")
                    row.set_child(widgets.VmRow(status))
                    self._attach_row_menu(row, status.name)
                    self.list_box.append(row)
                    self.rows[status.name] = row
            else:
                label = Gtk.Label(label="No matching VMs")
                label.add_css_class("dim")
                label.set_margin_top(18)
                self.list_box.append(Gtk.ListBoxRow(child=label))

        for status in visible:
            row = self.rows.get(status.name)
            if row is not None:
                child = row.get_child()
                if isinstance(child, widgets.VmRow):
                    child.update(status)

        self._sync_selection()

    def _sync_selection(self) -> None:
        self._syncing = True
        try:
            if not self.current:
                self.list_box.unselect_all()
                return
            for name, candidate in self.rows.items():
                should = name == self.current
                if candidate.is_selected() == should:
                    continue
                if should:
                    self.list_box.select_row(candidate)
                else:
                    self.list_box.unselect_row(candidate)
        finally:
            self._syncing = False

    def _attach_row_menu(self, row: Gtk.ListBoxRow, name: str) -> None:
        """Right-click actions for one VM, without selecting it first."""
        menu = Gio.Menu()
        menu.append("Open console", f"row.console::{name}")
        menu.append("Start", f"row.start::{name}")
        menu.append("Stop", f"row.stop::{name}")
        section = Gio.Menu()
        section.append("Clone…", f"row.clone::{name}")
        section.append("Export bundle", f"row.export::{name}")
        menu.append_section(None, section)
        danger = Gio.Menu()
        danger.append("Delete…", f"row.delete::{name}")
        menu.append_section(None, danger)

        popover = Gtk.PopoverMenu()
        popover.set_menu_model(menu)
        popover.set_has_arrow(False)

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("pressed", lambda *_: popover.popup())
        row.add_controller(gesture)

    def _row_action(self, action_name: str, name: str) -> None:
        """Handle a sidebar context-menu action."""
        if action_name == "console":
            url = lifecycle.console_url(name)
            if url:
                widgets.open_url(url)
            return
        if action_name == "start":
            widgets.run_task(
                self, lambda progress: lifecycle.start(name, progress=progress),
                label=f"Starting {name}",
            )
            return
        if action_name == "stop":
            widgets.run_task(
                self, lambda progress: lifecycle.stop(name, progress=progress),
                label=f"Stopping {name}",
            )
            return
        if action_name in {"clone", "export", "delete"}:
            # These need the detail pane's dialogs and confirmation wording.
            self.select_vm(name)
            pane = self.panes.get(name)
            if pane is None:
                return
            {"clone": pane._on_clone, "export": pane._on_export, "delete": pane._on_delete}[
                action_name
            ](None)

    def _on_row_selected(self, _list_box, row) -> None:
        if self._syncing or row is None:
            return
        for name, candidate in self.rows.items():
            if candidate is row:
                self.select_vm(name)
                return

    def _on_row_activated(self, _list_box, row) -> None:
        self._on_row_selected(_list_box, row)

    def select_vm(self, name: str | None) -> None:
        if self.current and self.current != name:
            existing = self.panes.get(self.current)
            if existing is not None:
                existing.close()
        self.current = name
        if not name:
            self.content.set_visible_child_name("empty")
            self.list_box.unselect_all()
            self.sidebar_empty.set_visible(not self.order)
            return
        pane = self.panes.get(name)
        if pane is None:
            pane = VmPane(self, name)
            self.panes[name] = pane
            self.content.add_named(pane, name)
        self.content.set_visible_child_name(name)
        self.sidebar_empty.set_visible(False)
        self._sync_selection()
        self._update_pane(name)

    def _update_pane(self, name: str) -> None:
        status = getattr(self, "statuses", {}).get(name)
        pane = self.panes.get(name)
        if status is None or pane is None:
            return
        try:
            pane.update(status)
        except Exception as exc:  # noqa: BLE001
            self.status_strip.set_idle(f"{name}: {exc}")

    def report(self, message: str) -> None:
        self.status_strip.set_idle(message)
        self.refresh()

    def new_vm(self) -> None:
        """Public entry point, used by the app's Ctrl+N action."""
        self._on_new(None)

    def _on_new(self, _button) -> None:
        from .newvm import NewVmDialog

        dialog = NewVmDialog(self)
        dialog.present()

    def _on_import(self, _button) -> None:
        chooser = Gtk.FileDialog()
        chooser.set_title("Import a vmhub bundle")
        chooser.set_filters(
            widgets.filter_store("vmhub bundles", ["*.vmhub.tar.gz"])
        )

        def on_open(source, result) -> None:
            try:
                file = chooser.open_finish(result)
            except Exception:
                return
            widgets.run_task(
                self,
                lambda progress: export.import_bundle(file.get_path(), progress=progress),
                label="Importing bundle",
                on_done=lambda vm: self.select_vm(vm),
            )

        chooser.open(self, None, on_open)

    def _on_start_all(self, _button) -> None:
        pending = [s.name for s in sorted(
            getattr(self, "statuses", {}).values(), key=lambda s: s.name
        ) if not s.powered]
        if not pending:
            return

        def work(progress):
            # Serial, not one thread per VM: each start allocates host ports and
            # writes a spec, so concurrent starts can claim the same port.
            started: list[str] = []
            for name in pending:
                try:
                    lifecycle.start(name, progress=progress)
                    started.append(name)
                except Exception as exc:  # noqa: BLE001
                    progress(f"{name}: {exc}")
            return started

        widgets.run_task(
            self, work, label=f"Starting {len(pending)} VM(s)",
            on_done=lambda done: self.report(f"Started {len(done)} VM(s)"),
        )

    def _on_stop_all(self, _button) -> None:
        widgets.run_task(
            self,
            lambda progress: lifecycle.stop_all(progress=progress),
            label="Stopping all VMs",
        )

    def _on_restore(self, _button: object = None) -> None:
        widgets.run_task(
            self,
            lambda progress: lifecycle.restore_run_state(progress=progress),
            label="Restoring VMs",
        )

    def _on_doctor(self, _button: object = None) -> None:
        from .. import autostart, paths
        from .. import disk as disk_mod
        from .. import podman as podman_mod

        try:
            lines = [
                f"podman: {podman_mod.version()}",
                f"rootless: {podman_mod.is_rootless()}",
                f"oci runtime: {podman_mod.oci_runtime()}",
                f"qemu-img: {disk_mod.binary_version()}",
                f"host /dev/kvm: {'readable' if podman_mod.host_has_kvm() else 'NOT ACCESSIBLE'}",
                f"init system: {autostart.detect_init()}",
                f"project root: {paths.project_root()}",
            ]
        except Exception as exc:
            lines = [f"error: {exc}"]
        dialog = Gtk.AlertDialog(
            message="Environment", detail="\n".join(lines),
            buttons=["Close"], default_button=0, cancel_button=0,
        )
        dialog.show(self)

    def _on_close(self, _window) -> bool:
        self._closing = True
        if self._refresh_tag:
            GLib.source_remove(self._refresh_tag)
            self._refresh_tag = 0
        for pane in self.panes.values():
            pane.close()
        return False
