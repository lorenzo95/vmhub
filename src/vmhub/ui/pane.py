from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import blueprints, disk, export, iso as isomod, lifecycle, paths, podman, rdp, registry, spec  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import style, widgets  # noqa: E402

DISK_TYPES = ["scsi", "blk", "ide", "sata", "nvme"]


class VmPane(Gtk.Box):
    def __init__(self, window: Gtk.Window, vm_name: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.vm = vm_name
        self.status: lifecycle.VmStatus | None = None
        self._snap_signature: tuple[str, ...] = ()
        self._log_tag = 0
        self._log_source = 0

        self.add_css_class("vmpane")
        self.set_vexpand(True)

        self._build_header()
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        self.append(self.notebook)
        self._build_console_tab()
        self._build_hardware_tab()
        self._build_snapshots_tab()
        self._build_actions_tab()
        self._build_logs_tab()

    def _build_header(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.set_margin_top(10)
        bar.set_margin_bottom(4)
        bar.set_margin_start(14)
        bar.set_margin_end(14)

        self.title = Gtk.Label()
        self.title.add_css_class("big")
        self.title.set_xalign(0.0)
        self.state_label = Gtk.Label()
        self.state_label.add_css_class("vmmeta")
        self.state_label.set_xalign(0.0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        text_box.append(self.title)
        text_box.append(self.state_label)
        bar.append(text_box)

        self.snapshot_button = Gtk.Button(label="Snapshot")
        self.snapshot_button.add_css_class("suggested-action")
        self.snapshot_button.set_tooltip_text("Take an instantaneous copy-on-write snapshot")
        self.snapshot_button.connect("clicked", self._on_snapshot)

        self.power_button = Gtk.Button(label="Start")
        self.power_button.add_css_class("suggested-action")
        self.power_button.connect("clicked", self._on_power)

        self.force_button = Gtk.Button(label="Force stop")
        self.force_button.add_css_class("destructive-action")
        self.force_button.set_tooltip_text(
            "SIGKILL the container immediately. The guest gets no shutdown and no "
            "cleanup runs, so its filesystem may be left inconsistent."
        )
        self.force_button.connect("clicked", self._on_force_stop)

        bar.append(self.snapshot_button)
        bar.append(self.power_button)
        bar.append(self.force_button)
        self.append(bar)
        self.header_bar = bar

        self.error_label = Gtk.Label()
        self.error_label.add_css_class("errrow")
        self.error_label.set_wrap(True)
        self.error_label.set_xalign(0.0)
        self.error_label.set_margin_start(14)
        self.error_label.set_margin_end(14)
        self.append(self.error_label)

    def _add_tab(self, title: str, child: Gtk.Widget) -> None:
        label = Gtk.Label(label=title)
        self.notebook.append_page(child, label)

    def _build_console_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        self.console_hint = Gtk.Label()
        self.console_hint.set_wrap(True)
        self.console_hint.set_xalign(0.0)
        box.append(self.console_hint)

        box.append(widgets.section("Web console"))
        self.console_row = widgets.copy_row("")
        box.append(self.console_row)
        open_button = Gtk.Button(label="Open console in browser")
        open_button.set_halign(Gtk.Align.START)
        open_button.connect("clicked", self._on_open_console)
        box.append(open_button)

        box.append(widgets.section("Shell access"))
        self.ssh_row = widgets.copy_row("")
        box.append(self.ssh_row)
        self.rdp_row = widgets.copy_row("")
        box.append(self.rdp_row)
        rdp_hint = Gtk.Label(
            label="The web console has no clipboard or drag-and-drop. "
            "Use RDP for a Windows guest if you need to copy files or text."
        )
        rdp_hint.add_css_class("hint")
        rdp_hint.set_wrap(True)
        rdp_hint.set_xalign(0.0)
        box.append(rdp_hint)

        self.rdp_missing_hint = Gtk.Label(
            label=(
                "No RDP port is published for this VM. RDP is only useful for a "
                "Windows guest; for Linux use the SSH row above."
            )
        )
        self.rdp_missing_hint.add_css_class("hint")
        self.rdp_missing_hint.set_wrap(True)
        self.rdp_missing_hint.set_xalign(0.0)
        self.rdp_enable_button = Gtk.Button(label="Publish an RDP port anyway")
        self.rdp_enable_button.set_halign(Gtk.Align.START)
        self.rdp_enable_button.connect("clicked", self._on_enable_rdp)
        self.rdp_test_button = Gtk.Button(label="Test connection")
        self.rdp_test_button.set_tooltip_text(
            "Speak RDP to the published port and report which hop fails"
        )
        self.rdp_test_button.set_halign(Gtk.Align.START)
        self.rdp_test_button.connect("clicked", self._on_test_rdp)
        self.remmina_button = Gtk.Button(label="Connect with Remmina")
        self.remmina_button.add_css_class("suggested-action")
        self.remmina_button.set_tooltip_text("Open this VM in Remmina over RDP")
        self.remmina_button.connect("clicked", self._on_remmina)
        self.remmina_button.set_halign(Gtk.Align.START)
        box.append(self.remmina_button)
        self.rdp_user_label = Gtk.Label()
        self.rdp_user_label.add_css_class("hint")
        self.rdp_user_label.set_wrap(True)
        self.rdp_user_label.set_xalign(0.0)
        box.append(self.rdp_user_label)
        box.append(self.rdp_test_button)
        box.append(self.rdp_missing_hint)
        box.append(self.rdp_enable_button)

        box.append(widgets.section("Install media"))
        media_hint = Gtk.Label(
            label="The install ISO is attached as a read-only CD-ROM and boots before "
            "the disk. Detach it once installation finishes, or the VM will boot it again."
        )
        media_hint.add_css_class("hint")
        media_hint.set_wrap(True)
        media_hint.set_xalign(0.0)
        box.append(media_hint)

        self.media_notice = Gtk.Label()
        self.media_notice.add_css_class("hint")
        self.media_notice.set_wrap(True)
        self.media_notice.set_xalign(0.0)
        box.append(self.media_notice)

        self.iso_rows: dict[str, dict[str, object]] = {}
        for slot in (isomod.INSTALL_SLOT, isomod.DRIVERS_SLOT):
            box.append(self._build_iso_row(slot))

        self._add_tab("Console", widgets.scroll(box))

    def _build_iso_row(self, slot: str) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_top(4)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=isomod.SLOT_LABELS[slot])
        title.set_xalign(0.0)
        title.set_hexpand(True)
        head.append(title)

        attach = Gtk.Button(label="Attach…")
        attach.connect("clicked", lambda _b, sl=slot: self._on_attach_iso(sl))
        detach = Gtk.Button(label="Detach")
        detach.connect("clicked", lambda _b, sl=slot: self._on_detach_iso(sl))
        detach.add_css_class("destructive-action")
        head.append(attach)
        head.append(detach)
        outer.append(head)

        detail = Gtk.Label()
        detail.add_css_class("hint")
        detail.set_xalign(0.0)
        detail.set_wrap(True)
        outer.append(detail)

        self.iso_rows[slot] = {"attach": attach, "detach": detach, "detail": detail}
        return outer

    def _refresh_iso_rows(self) -> None:
        try:
            entries = lifecycle.iso_status(self.vm)
        except Exception as exc:
            return
        for entry in entries:
            widgets_row = self.iso_rows.get(entry["slot"])
            if widgets_row is None:
                continue
            detail = widgets_row["detail"]
            detach = widgets_row["detach"]
            if not entry["attached"]:
                detail.set_text("not attached")
                detail.remove_css_class("errrow")
                detach.set_sensitive(False)
                continue
            detach.set_sensitive(True)
            info = entry["info"]
            if info is None:
                detail.set_text(f"MISSING — {entry['path']}")
                detail.add_css_class("errrow")
                continue
            detail.remove_css_class("errrow")
            detail.set_text(f"{info.describe()}\n{entry['path']}")

    def _iso_chooser(self, slot: str) -> None:
        chooser = Gtk.FileDialog()
        chooser.set_title(f"Choose an ISO for the {slot} slot")
        chooser.set_filters(
            widgets.filter_store("ISO images", ["*.iso", "*.img", "*.ISO", "*.IMG"])
        )
        for candidate in (Path.home() / "Documents/iso", Path.home() / "Downloads"):
            if candidate.is_dir():
                chooser.set_initial_folder(Gio.File.new_for_path(str(candidate)))
                break

        def on_open(source, result) -> None:
            try:
                selected = chooser.open_finish(result)
            except Exception:
                return
            path = selected.get_path()
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.attach_iso(self.vm, slot, Path(path), progress=progress),
                label=f"Attaching {Path(path).name}",
            )

        chooser.open(self.window, None, on_open)

    def _on_attach_iso(self, slot: str) -> None:
        self._iso_chooser(slot)

    def _on_detach_iso(self, slot: str) -> None:
        dialog = Gtk.AlertDialog(
            message=f"Detach the {slot} ISO?",
            detail=(
                "The ISO is no longer attached the next time the VM starts."
                if slot == isomod.INSTALL_SLOT
                else "The guest will no longer see the drivers CD-ROM."
            ),
            buttons=["Cancel", "Detach"],
            cancel_button=0,
            default_button=1,
        )

        def on_choice(_d, result) -> None:
            try:
                if dialog.choose_finish(result) != 1:
                    return
            except Exception:
                return
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.detach_iso(self.vm, slot, progress=progress),
                label=f"Detaching {slot} ISO",
            )

        dialog.choose(self.window, None, on_choice)

    def _on_test_rdp(self, _button: object = None) -> None:
        def work(_progress):
            return rdp.probe(self.vm)

        def done(result) -> bool:
            ok, message = result
            self.window.report(("connected: " if ok else "not reachable: ") + message)
            return False

        widgets.run_task(self.window, work, on_done=done, label="Testing RDP")

    def _on_enable_rdp(self, _button: object = None) -> None:
        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            if 3389 not in vm_spec.network.guest_ports:
                vm_spec.network.guest_ports = [*vm_spec.network.guest_ports, 3389]
            if not vm_spec.network.host_rdp:
                vm_spec.network.host_rdp = ports_mod.allocate("rdp")
            if not vm_spec.media.rdp_user:
                vm_spec.media.rdp_user = "user"
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"RDP will be published on port {vm_spec.network.host_rdp} after the next start")
            return True

        widgets.run_task(self.window, work, label="Publishing an RDP port")

    def _on_remmina(self, _button: object = None) -> None:
        try:
            vm_spec = registry.load_spec(self.vm)
        except Exception as exc:
            self.window.report(f"{self.vm}: {exc}")
            return
        if not rdp.target(self.vm).available:
            self.window.report(
                f"{self.vm} has no published RDP port - "
                f"add 3389 to its guest_ports first"
            )
            return
        if vm_spec.media.rdp_user:
            self._launch_remmina()
            return

        prompt = widgets.TextPrompt(
            self.window,
            "RDP account name",
            "NLA is enabled on the Windows guest, so a real account is required before "
            "the session opens. Enter the account you created during Windows setup - "
            "for a clone it is the same account as the template. Remmina will ask for "
            "the password and can save it.",
            placeholder="e.g. gero",
            accept_label="Connect",
        )

        def accept(user: str) -> None:
            def work(progress):
                fresh = registry.load_spec(self.vm)
                fresh.media.rdp_user = user
                registry.save_spec(fresh)
                progress(f"Saved RDP account {user!r}; launching Remmina")
                return rdp.launch(self.vm)

            widgets.run_task(
                self.window, work, label=f"Connecting to {self.vm} over RDP"
            )

        prompt.on_accept = accept

    def _launch_remmina(self) -> None:
        def work(progress):
            return rdp.launch(self.vm)

        widgets.run_task(
            self.window, work, label=f"Connecting to {self.vm} over RDP"
        )

    def _on_test_rdp(self, _button: object = None) -> None:
        def work(_progress):
            return rdp.probe(self.vm)

        def done(result) -> bool:
            ok, message = result
            self.window.report(("connected: " if ok else "not reachable: ") + message)
            return False

        widgets.run_task(self.window, work, on_done=done, label="Testing RDP")

    def _on_enable_rdp(self, _button: object = None) -> None:
        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            if 3389 not in vm_spec.network.guest_ports:
                vm_spec.network.guest_ports = [*vm_spec.network.guest_ports, 3389]
            if not vm_spec.network.host_rdp:
                vm_spec.network.host_rdp = ports_mod.allocate("rdp")
            if not vm_spec.media.rdp_user:
                vm_spec.media.rdp_user = "user"
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"RDP will be published on port {vm_spec.network.host_rdp} after the next start")
            return True

        widgets.run_task(self.window, work, label="Publishing an RDP port")

    def _on_remmina(self, _button) -> None:
        def work(_progress):
            return rdp.launch(self.vm)

        widgets.run_task(self.window, work, label="Launching Remmina")

    def _build_hardware_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        box.append(widgets.section("Resources"))
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.cpu_spin = Gtk.SpinButton.new_with_range(1, 128, 1)
        self.ram_entry = Gtk.Entry()
        self.disk_entry = Gtk.Entry()
        self.disk_type_drop = Gtk.DropDown.new_from_strings(DISK_TYPES)
        self.timeout_spin = Gtk.SpinButton.new_with_range(0, 600, 5)
        apply_button = Gtk.Button(label="Apply changes")
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", self._on_apply_resources)

        _hw_rows = [
            ("vCPUs", self.cpu_spin),
            ("RAM", self.ram_entry),
            ("Disk size", self.disk_entry),
            ("Disk bus", self.disk_type_drop),
            ("Shutdown (s)", self.timeout_spin),
        ]
        for row, (label_text, control) in enumerate(_hw_rows):
            lbl = Gtk.Label(label=label_text)
            lbl.set_xalign(0.0)
            lbl.set_size_request(120, -1)
            control.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(control, 1, row, 1, 1)
        grid.attach(apply_button, 1, len(_hw_rows), 1, 1)
        box.append(grid)
        self.guest_button = Gtk.Button(label="Query guest agent")
        self.guest_button.set_tooltip_text(
            "Ask qemu-guest-agent for the guest's IP, hostname and OS. "
            "Needs the agent installed in the guest."
        )
        self.guest_button.set_halign(Gtk.Align.START)
        self.guest_button.connect("clicked", self._on_query_guest)
        box.append(self.guest_button)
        self.guest_label = Gtk.Label()
        self.guest_label.add_css_class("hint")
        self.guest_label.set_wrap(True)
        self.guest_label.set_xalign(0.0)
        box.append(self.guest_label)

        apply_hint = Gtk.Label(
            label="Resource changes take effect on the next start. Disk size can grow "
            "but never shrinks; growing does not extend the guest partition."
        )
        apply_hint.add_css_class("hint")
        apply_hint.set_wrap(True)
        apply_hint.set_xalign(0.0)
        box.append(apply_hint)

        box.append(widgets.section("Details"))
        self.details_grid = widgets.key_value_grid([])
        box.append(self.details_grid)
        self._add_tab("Hardware", widgets.scroll(box))

    def _build_snapshots_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        take = Gtk.Button(label="Take snapshot")
        take.add_css_class("suggested-action")
        take.connect("clicked", self._on_snapshot)
        row.append(take)
        box.append(row)

        self.snapshot_info = Gtk.Label()
        self.snapshot_info.add_css_class("hint")
        self.snapshot_info.set_wrap(True)
        self.snapshot_info.set_xalign(0.0)
        box.append(self.snapshot_info)

        self.snapshot_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.snapshot_list.add_css_class("vmlist")
        box.append(widgets.scroll(self.snapshot_list))
        self._add_tab("Snapshots", box)

    def _build_actions_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        box.append(widgets.section("Template"))
        self.template_hint = Gtk.Label()
        self.template_hint.set_wrap(True)
        self.template_hint.set_xalign(0.0)
        self.template_hint.add_css_class("hint")
        box.append(self.template_hint)
        tmpl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.template_button = Gtk.Button(label="Mark as template")
        self.template_button.connect("clicked", self._on_toggle_template)
        clone_button = Gtk.Button(label="Clone…")
        clone_button.connect("clicked", self._on_clone)
        rebase_button = Gtk.Button(label="Rebase clones")
        rebase_button.set_tooltip_text(
            "Repoint every clone of this template at the template's current disk"
        )
        rebase_button.connect("clicked", self._on_rebase)
        tmpl_row.append(self.template_button)
        tmpl_row.append(clone_button)
        tmpl_row.append(rebase_button)
        tmpl_row.set_halign(Gtk.Align.START)
        box.append(tmpl_row)

        box.append(widgets.section("Portability"))
        port_hint = Gtk.Label(
            label="Export writes a self-contained bundle (standalone disk, manifest, "
            "checksums) that can be imported on another machine. Flatten resolves a "
            "clone's backing chain in place so it no longer depends on its template."
        )
        port_hint.set_wrap(True)
        port_hint.set_xalign(0.0)
        port_hint.add_css_class("hint")
        box.append(port_hint)
        port_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        export_button = Gtk.Button(label="Export bundle…")
        export_button.connect("clicked", self._on_export)
        flatten_button = Gtk.Button(label="Make portable (flatten)")
        flatten_button.connect("clicked", self._on_flatten)
        import_button = Gtk.Button(label="Import bundle…")
        import_button.connect("clicked", self._on_import)
        for button in (export_button, flatten_button, import_button):
            port_row.append(button)
        port_row.set_halign(Gtk.Align.START)
        box.append(port_row)

        box.append(widgets.section("Danger zone"))
        self.danger_hint = Gtk.Label()
        self.danger_hint.set_wrap(True)
        self.danger_hint.set_xalign(0.0)
        self.danger_hint.add_css_class("hint")
        box.append(self.danger_hint)
        delete_button = Gtk.Button(label="Delete VM…")
        delete_button.add_css_class("destructive-action")
        delete_button.set_halign(Gtk.Align.START)
        delete_button.connect("clicked", self._on_delete)
        box.append(delete_button)
        self._add_tab("Actions", widgets.scroll(box))

    def _build_logs_tab(self) -> None:
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.add_css_class("logview")
        buffer = self.log_view.get_buffer()
        self._log_buffer = buffer
        follow = Gtk.CheckButton(label="Follow")
        follow.set_active(True)
        follow.connect("toggled", lambda b: self._set_follow(b.get_active()))
        follow.set_halign(Gtk.Align.END)
        follow.set_margin_end(8)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(follow)
        scroller = widgets.scroll(self.log_view)
        scroller.set_vexpand(True)
        outer.append(scroller)
        self._add_tab("Logs", outer)
        self._follow = True
        self._start_follow()

    def _set_follow(self, value: bool) -> None:
        self._follow = value

    def _start_follow(self) -> None:
        self._log_source += 1
        source = self._log_source
        self._log_tag = GLib.timeout_add_seconds(2, self._poll_logs, source)

    def _poll_logs(self, source: int) -> bool:
        if source != self._log_source or not self.is_visible():
            return False
        if not self._follow or not podman.exists(self.vm):
            return True
        try:
            text = podman.logs(self.vm, tail=200)
        except Exception:
            return True
        cleaned = "\n".join(
            line for line in text.splitlines() if "\x1b[" not in line
        )
        self._log_buffer.set_text(cleaned[-40000:])
        end = self._log_buffer.get_end_iter()
        self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        return True

    def _stop_follow(self) -> None:
        self._log_source += 1
        if self._log_tag:
            GLib.source_remove(self._log_tag)
            self._log_tag = 0

    def update(self, status: lifecycle.VmStatus) -> None:
        self.status = status
        self.title.set_text(status.name)
        if not status.exists:
            self.error_label.set_text(
                "  ".join(status.errors)
                or "This VM is missing its vm.toml, so vmhub cannot read its settings."
            )
            self.error_label.set_visible(True)
            self.state_label.set_text("unreadable")
            self.force_button.set_visible(False)
            self.snapshot_button.set_sensitive(False)
            self.power_button.set_sensitive(False)
            self._clear_details()
            return
        try:
            self._update_details(status)
        except Exception as exc:  # noqa: BLE001
            self.error_label.set_text(f"Could not read this VM: {exc}")
            self.error_label.set_visible(True)

    def _clear_details(self) -> None:
        for row in self.iso_rows.values():
            detail = row["detail"]
            detail.set_text("unknown")
            row["detach"].set_sensitive(False)
        self.media_notice.set_visible(False)
        _replace_grid(self.details_grid, [])

    def _update_details(self, status: lifecycle.VmStatus) -> None:
        parts = [status.summary, f"{status.cpus} vCPU", status.ram, status.disk_size]
        if status.disk_actual:
            parts.append(f"{status.disk_actual} used")
        if status.template_source:
            parts.append(f"clone of {status.template_source}")
        self.state_label.set_text("  ·  ".join(parts))

        running = status.powered
        self.power_button.set_label("Stop" if running else "Start")
        self.power_button.remove_css_class("suggested-action")
        self.power_button.remove_css_class("destructive-action")
        self.power_button.add_css_class("destructive-action" if running else "suggested-action")
        self.force_button.set_visible(running)
        self.guest_button.set_visible(running)
        self.snapshot_button.set_sensitive(running)
        self.snapshot_button.set_tooltip_text(
            "Take an instantaneous copy-on-write snapshot"
            if running
            else "Start the VM to take a live snapshot"
        )

        if self.cpu_spin.get_value() != status.cpus:
            self.cpu_spin.set_value(status.cpus)
        if not self.ram_entry.has_focus():
            self.ram_entry.set_text(status.ram)
        if not self.disk_entry.has_focus():
            self.disk_entry.set_text(status.disk_size)
        try:
            current = registry.load_spec(status.name).shutdown.timeout
            if abs(self.timeout_spin.get_value() - current) > 0.5:
                self.timeout_spin.set_value(current)
        except Exception:
            pass
        if not self.disk_type_drop.get_selected():
            pass
        selected_type = self.disk_type_drop.get_selected()
        if 0 <= selected_type < len(DISK_TYPES) and DISK_TYPES[selected_type] != status.disk_type:
            if status.disk_type in DISK_TYPES:
                self.disk_type_drop.set_selected(DISK_TYPES.index(status.disk_type))

        url = status.console_url
        self.console_row.set_visible(bool(url))
        if url:
            widgets.set_copy_text(self.console_row, url)
            self.console_hint.set_text(
                f"The container serves the guest's display on port {status.viewer_port}. "
                "It works for installation and quick checks; it has no clipboard "
                "or drag-and-drop."
            )
        else:
            self.console_hint.set_text("No viewer port is published for this VM.")

        ssh = lifecycle.ssh_target(status.name)
        self.ssh_row.set_visible(bool(ssh))
        if ssh:
            widgets.set_copy_text(self.ssh_row, f"ssh {ssh}")

        target = rdp.target(status.name)
        has_rdp = target.available
        self.rdp_row.set_visible(has_rdp)
        if has_rdp:
            widgets.set_copy_text(self.rdp_row, target.uri())
        self.remmina_button.set_visible(has_rdp and rdp.remmina_available())
        self.rdp_missing_hint.set_visible(not has_rdp)
        self.rdp_enable_button.set_visible(not has_rdp)
        self.rdp_test_button.set_visible(has_rdp)
        user = registry.load_spec(status.name).media.rdp_user or ""
        self.rdp_user_label.set_visible(has_rdp and not user)
        if has_rdp and not user:
            self.rdp_user_label.set_text(
                "No RDP user is set, so the URI omits the username. Set the account you "
                "created during Windows setup with:  vmctl set "
                f"{status.name} media.rdp_user=YourName"
            )

        self._refresh_iso_rows()
        notices = list(getattr(status, "notices", []))
        self.media_notice.set_text("\n".join(notices))
        self.media_notice.set_visible(bool(notices))

        details = [
            ("Container", podman.container_name(status.name)),
            ("Image", registry.load_meta(status.name).image or "-"),
            ("UUID", registry.load_meta(status.name).uuid),
            ("MAC", registry.load_meta(status.name).mac),
            ("Disk bus", status.disk_type),
            ("Boot mode", status.boot_mode),
            ("Backing file", status.backing or "none (self-contained)"),
            ("Host ports", ", ".join(f"{h} → {g}" for h, g in sorted(status.ports.items())) or "-"),
            ("Guest ports", ", ".join(str(p) for p in status.guest_ports) or "-"),
            ("Boot media", status.boot_image or "-"),
            ("Guest-to-guest", ", ".join(status.peer_targets)
                if status.peer_targets
                else "not reachable (ports are loopback-only)"),
            ("Shutdown", f"timeout {registry.load_spec(status.name).shutdown.timeout}s (ACPI)"
                if not registry.load_spec(status.name).shutdown.skip_acpi
                else f"timeout {registry.load_spec(status.name).shutdown.timeout}s (no ACPI)"),
            ("Disk has data", "yes" if status.has_disk_data else "no (not installed yet)"),
            ("Disk path", str(paths.disk_path(status.name))),
            ("KVM", "enabled" if status.kvm else "disabled"),
        ]
        _replace_grid(self.details_grid, details)

        signature = tuple(status.snapshots)
        if signature != self._snap_signature:
            self._snap_signature = signature
            self._rebuild_snapshot_list(status)
        if status.has_disk and status.snapshots:
            self.snapshot_info.set_text(
                f"{len(status.snapshots)} snapshot(s). Taking one is instantaneous and "
                "costs almost no disk. Reverting stops the VM and discards every snapshot "
                "taken after the one you pick."
            )
        elif not status.has_disk:
            self.snapshot_info.set_text("This VM has no disk yet.")
        else:
            self.snapshot_info.set_text(
                "No snapshots. Snapshots are copy-on-write, so they are cheap and instant."
            )

        if status.is_template:
            self.template_button.set_label("Remove template mark")
            kids = ", ".join(status.dependents) or "none yet"
            self.template_hint.set_text(
                f"This VM is a template. Clones: {kids}. "
                "A template cannot run while its clones are running, because the clone "
                "holds a read lock on the template's disk."
            )
        else:
            self.template_button.set_label("Mark as template")
            if status.template_source:
                self.template_hint.set_text(
                    f"This VM is a clone of {status.template_source}. Flatten it to become "
                    "independent, or delete it — the template disk is shared until then."
                )
            else:
                self.template_hint.set_text(
                    "Mark this VM as a template to make instant, copy-on-write clones from it. "
                    "Stop it first."
                )

        self.danger_hint.set_text(
            f"Deleting removes the VM, its container and its disk. "
            f"A clone costs almost nothing on disk, so deleting a clone never touches the "
            f"template."
        )
        self.error_label.set_text("  ".join(status.errors))
        self.error_label.set_visible(bool(status.errors))

    def _rebuild_snapshot_list(self, status: lifecycle.VmStatus) -> None:
        child = self.snapshot_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.snapshot_list.remove(child)
            child = nxt
        if not status.snapshots:
            label = Gtk.Label(label="No snapshots yet.")
            label.add_css_class("dim")
            label.set_margin_top(10)
            self.snapshot_list.append(label)
            return
        for name in status.snapshots:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_top(4)
            row.set_margin_bottom(4)
            label = Gtk.Label(label=name)
            label.set_xalign(0.0)
            label.set_hexpand(True)
            row.append(label)
            revert_button = Gtk.Button(label="Revert")
            revert_button.add_css_class("destructive-action")
            revert_button.connect("clicked", lambda _b, n=name: self._on_revert(n))
            delete_button = Gtk.Button(label="Delete")
            delete_button.connect("clicked", lambda _b, n=name: self._on_delete_snapshot(n))
            row.append(revert_button)
            row.append(delete_button)
            self.snapshot_list.append(row)

    def _on_power(self, _button) -> None:
        status = self.status
        if status is None:
            return
        if status.powered:
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.stop(self.vm, progress=progress),
                label=f"Stopping {self.vm}",
            )
        else:
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.start(self.vm, progress=progress),
                label=f"Starting {self.vm}",
            )

    def _on_force_stop(self, _button: object = None) -> None:
        dialog = Gtk.AlertDialog(
            message=f"Force stop {self.vm}?",
            detail=(
                "The container is killed immediately. The guest is not asked to shut "
                "down and nothing is cleaned up, so its filesystem may be left "
                "inconsistent. Use this only if a graceful stop is hanging."
            ),
            buttons=["Cancel", "Force stop"],
            cancel_button=0,
            default_button=1,
        )

        def on_choice(_d, result) -> None:
            try:
                if dialog.choose_finish(result) != 1:
                    return
            except Exception:
                return
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.stop(self.vm, mode="force", progress=progress),
                label=f"Force stopping {self.vm}",
            )

        dialog.choose(self.window, None, on_choice)

    def _on_snapshot(self, _button) -> None:
        def accept(name: str) -> None:
            if not name:
                return
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.take_snapshot(self.vm, name, progress=progress),
                label=f"Snapshot {name}",
            )

        widgets.prompt_text(
            self.window,
            "Take a snapshot",
            "Name this point-in-time snapshot. It is instantaneous and copy-on-write.",
            on_accept=accept,
        )

    def _on_revert(self, name: str) -> None:
        dialog = Gtk.AlertDialog(
            message=f"Revert {self.vm} to '{name}'?",
            detail=(
                "The VM will be stopped, the disk restored to this snapshot, and every "
                "snapshot taken after it will be discarded. The VM restarts afterwards."
            ),
            buttons=["Cancel", "Revert"],
            cancel_button=0,
            default_button=1,
        )

        def on_choice(_d, result) -> None:
            try:
                if dialog.choose_finish(result) == 1:
                    widgets.run_task(
                        self.window,
                        lambda progress: lifecycle.revert(self.vm, name, progress=progress),
                        label=f"Reverting to {name}",
                    )
            except Exception:
                pass

        dialog.choose(self.window, None, on_choice)

    def _on_delete_snapshot(self, name: str) -> None:
        widgets.run_task(
            self.window,
            lambda progress: lifecycle.delete_snapshot(self.vm, name, progress=progress),
            label=f"Deleting snapshot {name}",
        )

    def _on_query_guest(self, _button: object = None) -> None:
        def work(_progress):
            return lifecycle.guest_info(self.vm)

        def done(info) -> bool:
            self.guest_label.set_text(info.describe())
            self.guest_label.set_visible(True)
            return False

        widgets.run_task(self.window, work, on_done=done, label="Querying guest agent")

    def _on_apply_resources(self, _button) -> None:
        def work(progress):
            vm_spec = registry.load_spec(self.vm)
            before = (vm_spec.resources.cpus, vm_spec.resources.disk_bytes)
            vm_spec.resources.cpus = int(self.cpu_spin.get_value())
            vm_spec.resources.ram = self.ram_entry.get_text().strip()
            selected = self.disk_type_drop.get_selected()
            if 0 <= selected < len(DISK_TYPES):
                vm_spec.resources.disk_type = DISK_TYPES[selected]
            vm_spec.shutdown.timeout = int(self.timeout_spin.get_value())
            requested = self.disk_entry.get_text().strip()
            vm_spec.validate()
            registry.save_spec(vm_spec)
            if requested and spec.parse_size(requested) > before[1]:
                progress(f"growing disk to {requested}…")
                lifecycle.resize(self.vm, requested, progress=progress)
            return True

        widgets.run_task(self.window, work, label="Applying resource changes")

    def _on_toggle_template(self, _button) -> None:
        if self.status is None:
            return
        if self.status.is_template:
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.unmark_template(self.vm, progress=progress),
                label="Removing template mark",
            )
            return
        dialog = Gtk.AlertDialog(
            message=f"Mark {self.vm} as a template?",
            detail=(
                "Clones made from it share its disk copy-on-write, so they start instantly "
                "and cost almost nothing. Stop the VM first. A template cannot run while "
                "its clones are running."
            ),
            buttons=["Cancel", "Mark"],
            cancel_button=0,
            default_button=1,
        )

        def on_choice(_d, result) -> None:
            try:
                if dialog.choose_finish(result) != 1:
                    return
            except Exception:
                return
            def mark(progress):
                if podman.is_running(self.vm):
                    lifecycle.stop(self.vm, progress=progress)
                lifecycle.mark_template(self.vm, progress=progress)
                return True

            widgets.run_task(self.window, mark, label="Marking as template")

        dialog.choose(self.window, None, on_choice)

    def _on_clone(self, _button: object = None) -> None:
        if self.status is None:
            return
        source = self.vm

        linked = Gtk.CheckButton(label="Linked clone (instant, shares storage)")
        linked.set_active(True)
        full = Gtk.CheckButton(label="Full clone (independent, copies the disk)")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.append(linked)
        box.append(full)

        prompt = widgets.TextPrompt(
            self.window,
            f"Clone {source}",
            "Name the clone.",
            default=f"{source}-copy",
            placeholder="clone name",
            accept_label="Clone",
            extra=box,
        )

        def accept(target: str) -> None:
            mode = "full" if full.get_active() else "linked"
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.clone(
                    target, source, mode=mode, progress=progress
                ),
                label=f"Cloning {source} to {target} ({mode})",
                on_done=lambda _r: self.window.select_vm(target),
            )

        prompt.on_accept = accept

    def _on_rebase(self, _button) -> None:
        widgets.run_task(
            self.window,
            lambda progress: lifecycle.rebase_all(self.vm, progress=progress),
            label="Rebasing clones",
            on_done=lambda updated: self.window.report(
                f"Rebased {len(updated)} clone(s) onto {self.vm}"
                if updated
                else "No clones to rebase"
            ),
        )

    def _on_flatten(self, _button) -> None:
        widgets.run_task(
            self.window,
            lambda progress: lifecycle.flatten(self.vm, progress=progress),
            label="Flattening",
        )

    def _on_export(self, _button) -> None:
        widgets.run_task(
            self.window,
            lambda progress: export.export_vm(self.vm, progress=progress),
            label="Exporting bundle",
            on_done=lambda path: self.window.report(f"Exported to {path}"),
        )

    def _on_import(self, _button) -> None:
        chooser = Gtk.FileDialog()
        chooser.set_title("Import a vmhub bundle")
        chooser.set_filters(
            widgets.filter_store("vmhub bundles", ["*.vmhub.tar.gz"])
        )

        def on_open(source, result) -> None:
            try:
                selected = chooser.open_finish(result)
            except Exception:
                return
            path = selected.get_path()
            widgets.run_task(
                self.window,
                lambda progress: export.import_bundle(path, progress=progress),
                label="Importing bundle",
                on_done=lambda name: self.window.select_vm(name),
            )

        chooser.open(self.window, None, on_open)

    def _on_delete(self, _button) -> None:
        dialog = Gtk.AlertDialog(
            message=f"Delete {self.vm}?",
            detail="This removes the VM, its container and its disk. This cannot be undone.",
            buttons=["Cancel", "Delete"],
            cancel_button=0,
            default_button=1,
        )

        def on_choice(_d, result) -> None:
            try:
                if dialog.choose_finish(result) != 1:
                    return
            except Exception:
                return
            widgets.run_task(
                self.window,
                lambda progress: lifecycle.remove(self.vm, progress=progress),
                label=f"Deleting {self.vm}",
                on_done=lambda _r: self.window.select_vm(None),
            )

        dialog.choose(self.window, None, on_choice)

    def _on_open_console(self, _button) -> None:
        if self.status and self.status.console_url:
            widgets.open_url(self.status.console_url)

    def close(self) -> None:
        self._stop_follow()


def _replace_grid(grid: Gtk.Grid, pairs: list[tuple[str, str]]) -> None:
    while True:
        child = grid.get_first_child()
        if child is None:
            break
        nxt = child.get_next_sibling()
        grid.remove(child)
        child = nxt
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
