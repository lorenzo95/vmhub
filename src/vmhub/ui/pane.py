from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import blueprints, disk, export, iso as isomod, lifecycle, paths, podman, rdp, registry, settings, spec  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import style, widgets, workers  # noqa: E402

DISK_TYPES = ["scsi", "blk", "ide", "sata", "nvme"]

# The three ports vmhub drives itself: the title shown for the row, and the
# guest port behind it. web is the container's own display rather than a
# guest service, which is why it is never gated on network.guest_ports.
ROLE_ROWS = {
    "web": ("Web console", 8006),
    "ssh": ("SSH", 22),
    "rdp": ("RDP", 3389),
}


class VmPane(Gtk.Box):
    def __init__(self, window: Gtk.Window, vm_name: str, page: int = 0) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.vm = vm_name
        self.status: lifecycle.VmStatus | None = None
        self._snap_signature: tuple[str, ...] = ()
        self._log_tag = 0
        self._log_source = 0
        self._log_reading = False
        # Whether the user has an unapplied edit in the hardware controls,
        # and whether we are the ones writing into them right now.
        self._dirty = False
        self._loading = False

        self.add_css_class("vmpane")
        self.set_vexpand(True)

        self._build_header()
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        self.append(self.notebook)
        self._build_console_tab()
        self._build_hardware_tab()
        self._build_ports_tab()
        self._build_details_tab()
        self._build_snapshots_tab()
        self._build_logs_tab()

        # Open on the tab the window asks for. Every VM has the same tabs, so
        # this cannot land out of range in practice, but a pane built before
        # the last tab existed should not raise over it.
        self.notebook.set_current_page(
            max(0, min(page, self.notebook.get_n_pages() - 1))
        )

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

        # Delete lost its button when the Actions tab was dissolved and was
        # reachable only from the sidebar's right-click menu, which is not
        # somewhere to keep the one action that cannot be undone.
        self.delete_button = Gtk.Button(label="Delete…")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.set_tooltip_text(
            "Remove this VM, its container and its disk. This cannot be undone."
        )
        self.delete_button.connect("clicked", self._on_delete)

        bar.append(self.snapshot_button)
        bar.append(self.power_button)
        bar.append(self.force_button)
        bar.append(self.delete_button)
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
                "Windows guest; for Linux use the SSH row above. Publish one from "
                "the Ports section on the Info tab."
            )
        )
        self.rdp_missing_hint.add_css_class("hint")
        self.rdp_missing_hint.set_wrap(True)
        self.rdp_missing_hint.set_xalign(0.0)
        self.remmina_button = Gtk.Button(label="Connect with Remmina")
        self.remmina_button.add_css_class("suggested-action")
        self.remmina_button.set_tooltip_text("Open this VM in Remmina over RDP")
        self.remmina_button.connect("clicked", self._on_remmina)
        self.remmina_button.set_halign(Gtk.Align.START)
        box.append(self.remmina_button)
        # Stays put whether or not it can be used. It used to disappear
        # whenever there was no RDP port or Remmina was not installed, which
        # left nothing on screen to say the feature existed at all.
        self.rdp_reason = Gtk.Label()
        self.rdp_reason.add_css_class("hint")
        self.rdp_reason.set_wrap(True)
        self.rdp_reason.set_xalign(0.0)
        box.append(self.rdp_reason)

        # ---- RDP options ----
        # The "Test connection" button that used to live here is gone: it spoke
        # RDP to the port and reported into the status bar, which the 4-second
        # poll overwrote, so in practice it said nothing. These are the two
        # settings Remmina actually needs.
        box.append(widgets.section("RDP options"))

        self.rdp_user_entry = Gtk.Entry()
        self.rdp_user_entry.set_hexpand(True)
        self.rdp_user_entry.set_placeholder_text("Account you created during Windows setup")
        self.rdp_user_entry.set_tooltip_text(
            "Used for the Remmina profile and the RDP URI. Leave empty to fall back "
            "to the blueprint, then to the app-wide default."
        )
        self.rdp_user_entry.connect("changed", self._on_rdp_user_changed)
        box.append(widgets.field("Account", self.rdp_user_entry))

        self.share_entry = Gtk.Entry()
        self.share_entry.set_hexpand(True)
        self.share_entry.set_placeholder_text("Folder to map into the session, e.g. ~/Shared")
        self.share_entry.set_tooltip_text(
            "Mapped as a drive by RDP. Leave empty to use the app-wide default; "
            "set it to '.' to map nothing for this VM."
        )
        self.share_entry.connect("changed", self._on_share_changed)
        share_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        share_row.append(self.share_entry)
        browse = Gtk.Button(label="Browse…")
        browse.set_tooltip_text("Pick the folder to map into every RDP session")
        browse.connect("clicked", self._on_share_browse)
        share_row.append(browse)
        box.append(widgets.field("Shared folder", share_row))

        self.share_source = Gtk.Label()
        self.share_source.add_css_class("hint")
        self.share_source.set_wrap(True)
        self.share_source.set_xalign(0.0)
        box.append(self.share_source)
        # Set while a refresh pushes values into the entries, so those writes
        # are not mistaken for the user editing them.
        self._rdp_loading = False

        box.append(widgets.section("Install media"))
        media_hint = Gtk.Label(
            label="The install ISO is attached as a read-only CD-ROM and boots before "
            "the disk. Detach it once installation finishes, or the VM will boot it again."
        )
        media_hint.add_css_class("hint")
        media_hint.set_wrap(True)
        media_hint.set_xalign(0.0)
        box.append(media_hint)

        # This used to exist only inside a notice, and a notice only appears
        # when there is genuinely an ISO to detach or a cached fallback to
        # drop. That is the right condition and the wrong delivery: on a VM
        # with nothing to finish, the button simply was not there, with
        # nothing to say so. It now sits here permanently and says why it is
        # unavailable.
        self.finish_install_button = Gtk.Button(label="Finish install")
        self.finish_install_button.add_css_class("suggested-action")
        self.finish_install_button.set_halign(Gtk.Align.START)
        self.finish_install_button.connect("clicked", self._on_finish_install)
        box.append(self.finish_install_button)

        self.notice_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.notice_box)

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

    def _sync_entry(self, entry: Gtk.Entry, effective: str, saved: str) -> None:
        """Show `effective` in `entry`, unless the user is editing it.

        `saved` is what is on disk for this VM's own setting; `effective` is
        what is in force, which may come from the blueprint or the app-wide
        default instead. An entry is refreshed only when it is in step with
        disk (no edit pending) and does not already show `effective`.
        """
        current = entry.get_text().strip()
        if current == saved.strip():
            # The field agrees with disk, so whatever was typed has been saved
            # and the next refresh is free to write.
            entry._vmhub_pending = False
        elif not getattr(entry, "_vmhub_pending", False) and current != effective:
            entry.set_text(effective)

    def _on_rdp_user_changed(self, entry: Gtk.Entry | None) -> None:
        """Save the account on the spot. It is one field and one keystroke to
        undo, and making it a separate step is how it went unset before."""
        # A handler sweep calls this with a placeholder argument; there is no
        # entry to read, and there is nothing to save.
        if entry is None or self._rdp_loading:
            return
        text = entry.get_text().strip()
        # The field now holds an edit that disk does not have yet. Mark it, so
        # update() leaves it alone until the save below lands -- otherwise the
        # next poll writes the previous value back over the caret.
        entry._vmhub_pending = True

        def work(_progress):
            fresh = registry.load_spec(self.vm)
            if fresh.media.rdp_user == text:
                return False
            fresh.media.rdp_user = text
            registry.save_spec(fresh)
            return True

        widgets.run_task(self.window, work, label=f"Saving RDP account for {self.vm}")

    def _on_share_changed(self, entry: Gtk.Entry | None) -> None:
        if entry is None or self._rdp_loading:
            return
        text = entry.get_text().strip()
        # "." is how a VM opts out of a shared default. Empty cannot mean "map
        # nothing", because empty means "use the default".
        value = "" if text == "." else text
        # Same reasoning as the account field: mark the field as holding an edit
        # the poll must not overwrite.
        entry._vmhub_pending = True

        def work(_progress):
            fresh = registry.load_spec(self.vm)
            if fresh.media.share_path == value:
                return False
            fresh.media.share_path = value
            registry.save_spec(fresh)
            return True

        widgets.run_task(self.window, work, label=f"Saving shared folder for {self.vm}")

    def _on_share_browse(self, _button: object = None) -> None:
        chooser = Gtk.FileDialog(title="Folder to map into the RDP session")
        current = self.share_entry.get_text().strip()
        if current and current != ".":
            try:
                chooser.set_initial_folder(
                    Gio.File.parse_name(str(Path(current).expanduser()))
                )
            except Exception:
                # A path that no longer resolves must not stop the chooser
                # from opening.
                pass

        def on_selected(source, result) -> None:
            # Gtk.FileDialog is an async API: PyGObject calls this with
            # (dialog, result) and the folder comes out of select_folder_finish.
            # This used to be a one-argument done(folder), which raised TypeError
            # the moment a folder was chosen -- and the traceback went nowhere,
            # so the button looked like it selected nothing. Every other chooser
            # in the app (ISO attach, bundle import) already had this shape.
            try:
                folder = chooser.select_folder_finish(result)
            except Exception:
                return
            if folder is None:
                return
            path = folder.get_path()
            if path:
                # set_text() emits "changed", which marks the field pending and
                # saves it. Choosing a folder also used to look like it did
                # nothing, because the dialog had taken the focus and the poll
                # put the old path straight back.
                self.share_entry._vmhub_pending = True
                self.share_entry.set_text(path)

        chooser.select_folder(self.window, None, on_selected)

    def _refresh_notices(self, notices: list) -> None:
        """One row per notice, with the action that resolves it on the row.

        A wall of text said something was wrong and then, further down,
        offered the fix. Putting the button on the line that says what is
        wrong puts the two together, and a notice with no action still
        gets a row of its own instead of a line in a paragraph.
        """
        self._clear_box(self.notice_box)
        for note in notices:
            self.notice_box.append(self._notice_row(note))
        self.notice_box.set_visible(bool(notices))

    def _notice_row(self, note) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Label(label=str(note))
        text.add_css_class("hint")
        text.set_wrap(True)
        text.set_xalign(0.0)
        text.set_hexpand(True)
        row.append(text)
        # The action button that used to hang off this row moved to the
        # Install media section, where it is always present rather than
        # appearing and disappearing with the notice that describes it.
        return row

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        while True:
            child = box.get_first_child()
            if child is None:
                break
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _refresh_ports(self) -> None:
        self._clear_box(self.roles_box)
        self._clear_box(self.ports_box)
        try:
            net = registry.load_spec(self.vm).network
        except VmhubError as exc:
            label = Gtk.Label(label=str(exc))
            label.add_css_class("hint")
            self.ports_box.append(label)
            return
        for role in spec.PORT_ROLES:
            self.roles_box.append(self._role_row(role, net.by_role(role)))
        extras = [port for port in net.ports if not port.role]
        if extras:
            self.ports_box.append(widgets.section("Also forwarding"))
        for port in extras:
            self.ports_box.append(self._port_row(port))

    def _role_row(self, role: str, port: spec.Port | None) -> Gtk.Widget:
        title, guest = ROLE_ROWS[role]
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=title)
        name.set_xalign(0.0)
        name.set_size_request(150, -1)
        row.append(name)
        if port and port.host:
            text = Gtk.Label(label=f"host {port.host} → guest {port.guest}")
            text.add_css_class("mono")
            text.set_xalign(0.0)
            text.set_hexpand(True)
            row.append(text)
            if role == "rdp":
                test = Gtk.Button(label="Open in Remmina")
                test.set_tooltip_text("Write the Remmina profile and open this VM")
                test.connect("clicked", self._on_remmina)
                row.append(test)
            change = Gtk.Button(label="Change…")
            change.connect("clicked", lambda _b, r=role: self._on_change_role(r))
            row.append(change)
            remove = Gtk.Button(label="Remove")
            remove.add_css_class("destructive-action")
            remove.connect("clicked", lambda _b, r=role: self._on_remove_role(r))
            row.append(remove)
        else:
            text = Gtk.Label(label=f"not published (guest {guest})")
            text.add_css_class("hint")
            text.set_xalign(0.0)
            text.set_hexpand(True)
            row.append(text)
            add = Gtk.Button(label="Publish")
            add.set_tooltip_text(f"Pick a free host port for guest {guest}")
            add.connect("clicked", lambda _b, r=role: self._on_add_role(r))
            row.append(add)
        return row

    def _port_row(self, port: spec.Port) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=port.name or f"guest {port.guest}")
        name.set_xalign(0.0)
        name.set_size_request(150, -1)
        if port.host:
            text = Gtk.Label(label=f"host {port.host} → guest {port.guest}")
        else:
            text = Gtk.Label(label=f"not published (guest {port.guest})")
            text.add_css_class("hint")
        text.add_css_class("mono")
        text.set_xalign(0.0)
        text.set_hexpand(True)
        change = Gtk.Button(label="Change…")
        change.connect("clicked", lambda _b, p=port: self._on_change_port(p))
        remove = Gtk.Button(label="Remove")
        remove.add_css_class("destructive-action")
        remove.connect("clicked", lambda _b, p=port: self._on_remove_port(p))
        row.append(name)
        row.append(text)
        row.append(change)
        row.append(remove)
        return row

    def _parse_port(self, text: str, what: str, *, blank_ok: bool = False) -> int | None:
        """A port the user typed, or None after saying what was wrong."""
        raw = text.strip()
        if not raw:
            if blank_ok:
                return 0
            self.window.report(f"Enter a {what}.")
            return None
        try:
            port = int(raw)
        except ValueError:
            self.window.report(f"{raw!r} is not a {what}.")
            return None
        if not 1 <= port <= 65535:
            self.window.report(f"A {what} must be between 1 and 65535, not {port}.")
            return None
        return port

    def _on_add_role(self, role: str | None = None) -> None:
        # A bare call has no row behind it; the click handlers always pass
        # the role they were built for.
        if not role:
            return
        title, guest = ROLE_ROWS[role]

        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            net = vm_spec.network
            if role != "web" and guest not in net.guest_ports:
                # A guest service the container is not forwarding never answers.
                net.guest_ports = sorted({*net.guest_ports, guest})
            host = ports_mod.allocate(
                role, also_wanted=set(ports_mod.used_by_vms(self.vm)) | set(net.published())
            )
            net.set_role(role, guest, host)
            # No invented RDP account. This used to set media.rdp_user to the
            # literal string "user", which meant the Remmina prompt -- the one
            # thing that could ask for the real account -- never appeared, and
            # the session simply failed NLA. rdp.rdp_user() falls back to the
            # blueprint and then to the app-wide default, and stays blank
            # rather than guessing.
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"{title} will be on host {host} after the next start")
            return True

        widgets.run_task(self.window, work, label=f"Publishing {title.lower()}")

    def _port_dialog(
        self,
        heading: str,
        body: str,
        *,
        guest: int = 0,
        host: int = 0,
        name: str = "",
        show_label: bool,
        host_optional: bool,
        prefill: bool = True,
        on_save: Callable[[int, int, str], None],
    ) -> None:
        """Ask for a port's guest number, host number and label.

        Every one of them is editable. A guest number is as easy to mistype
        as a host one, and before this the only way to correct it was to
        delete the entry and add it again -- which loses the port and any
        habit the user had built up around it.
        """
        guest_entry = Gtk.Entry()
        guest_entry.set_name("vmhub.port.guest")
        host_entry = Gtk.Entry()
        host_entry.set_name("vmhub.port.host")
        name_entry = Gtk.Entry()
        name_entry.set_name("vmhub.port.name")
        if prefill:
            guest_entry.set_text(str(guest) if guest else "")
            host_entry.set_text(str(host) if host else "")
            name_entry.set_text(name)
        else:
            guest_entry.set_placeholder_text("5432")
            host_entry.set_placeholder_text(
                "leave empty to pick a free one" if host_optional else "e.g. 15432"
            )
            name_entry.set_placeholder_text("optional label")
        rows = [("Guest port", guest_entry), ("Host port", host_entry)]
        if show_label:
            rows.insert(1, ("Label", name_entry))
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        for row, (caption, entry) in enumerate(rows):
            key = Gtk.Label(label=caption)
            key.set_xalign(0.0)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)

        def accept(_text: str) -> None:
            # Read the entries here, on the main loop. The worker runs off
            # it, and GTK is not safe to touch from there.
            new_guest = self._parse_port(guest_entry.get_text(), "guest port")
            if new_guest is None:
                return
            new_host = self._parse_port(
                host_entry.get_text(), "host port", blank_ok=host_optional
            )
            if new_host is None:
                return
            new_name = name_entry.get_text().strip() if show_label else name
            on_save(new_guest, new_host, new_name)

        dialog = widgets.prompt_text(
            self.window, heading, body, on_accept=accept, extra=grid, accept_label="Save"
        )
        for entry in (guest_entry, host_entry, name_entry):
            entry.connect("activate", lambda _e: dialog.accept_button.emit("clicked"))
        return dialog

    def _on_change_role(self, role: str | None = None) -> None:
        if not role:
            return
        title, default_guest = ROLE_ROWS[role]
        port = registry.load_spec(self.vm).network.by_role(role)
        self._port_dialog(
            f"Edit the {title} port",
            "The port inside the VM, and the port on this machine that reaches it.",
            guest=port.guest if port else default_guest,
            host=port.host if port else 0,
            show_label=False,
            host_optional=False,
            on_save=lambda guest, host, _name: self._apply_role_edit(role, guest, host),
        )

    def _apply_role_edit(self, role: str, guest: int, host: int) -> None:
        title, _default_guest = ROLE_ROWS[role]

        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            net = vm_spec.network
            entry = net.by_role(role)
            old_guest = entry.guest if entry else 0
            if ports_mod.host_taken_by_other(net, host, exclude=entry):
                raise VmhubError(f"host port {host} is already published by this VM")
            if entry is None:
                net.ports.append(spec.Port(guest=guest, host=host, role=role))
            else:
                entry.guest = guest
                entry.host = host
            if role != "web" and guest not in net.guest_ports:
                net.guest_ports = sorted({*net.guest_ports, guest})
            if old_guest and old_guest != guest:
                ports_mod.prune_guest_forward(net, old_guest)
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"{title} will be guest {guest} on host {host} after the next start")
            return True

        widgets.run_task(self.window, work, label="Saving the port")

    def _on_remove_role(self, role: str | None = None) -> None:
        if not role:
            return
        title, _guest = ROLE_ROWS[role]

        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            port = vm_spec.network.by_role(role)
            if port is not None:
                port.host = 0
                vm_spec.network.ports.remove(port)
            ports_mod.prune_guest_forward(vm_spec.network, guest)
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"{title} will stop being published after the next start")
            return True

        widgets.run_task(self.window, work, label="Removing a port")

    def _on_add_port(self, _button: object = None) -> None:
        def on_save(guest: int, host: int, name: str) -> None:
            def work(progress):
                from .. import ports as ports_mod

                vm_spec = registry.load_spec(self.vm)
                net = vm_spec.network
                if host and host in net.published():
                    raise VmhubError(
                        f"host port {host} is already published by this VM"
                    )
                # A separate name from the one captured above: rebinding
                # `host` here would make it local to work() and the read
                # above would raise UnboundLocalError.
                chosen = host
                if not chosen:
                    chosen = ports_mod.allocate_any(
                        also_wanted=set(ports_mod.used_by_vms(self.vm)) | set(net.published())
                    )
                net.ports.append(spec.Port(guest=guest, host=chosen, name=name))
                # Forward it in the same breath, so the row is not a promise
                # the container has no way to keep.
                net.guest_ports = sorted({*net.guest_ports, guest})
                vm_spec.validate()
                registry.save_spec(vm_spec)
                progress(f"guest {guest} will be on host {chosen} after the next start")
                return True

            widgets.run_task(self.window, work, label="Adding a port")

        return self._port_dialog(
            "Forward another port",
            "Publish a port of the VM's own on a port of this machine.",
            show_label=True,
            host_optional=True,
            prefill=False,
            on_save=on_save,
        )

    def _on_change_port(self, port: spec.Port | None = None) -> None:
        if port is None:
            return
        guest, old_host, name = port.guest, port.host, port.name
        self._port_dialog(
            f"Edit {name or f'guest {guest}'}",
            "The port inside the VM, and the port on this machine that reaches it.",
            guest=guest,
            host=old_host,
            name=name,
            show_label=True,
            host_optional=False,
            on_save=lambda new_guest, new_host, new_name: self._apply_port_edit(
                (guest, old_host, name), new_guest, new_host, new_name
            ),
        )

    def _apply_port_edit(
        self, expected: tuple[int, int, str], guest: int, host: int, name: str
    ) -> None:
        old_guest, old_host, old_name = expected

        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            net = vm_spec.network
            entry = self._find_port(net, old_guest, old_host, old_name)
            if entry is None:
                raise VmhubError("the port list changed; try again")
            if ports_mod.host_taken_by_other(net, host, exclude=entry):
                raise VmhubError(f"host port {host} is already published by this VM")
            entry.guest = guest
            entry.host = host
            entry.name = name
            if guest not in net.guest_ports:
                net.guest_ports = sorted({*net.guest_ports, guest})
            if old_guest != guest:
                ports_mod.prune_guest_forward(net, old_guest)
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"guest {guest} will be on host {host} after the next start")
            return True

        widgets.run_task(self.window, work, label="Saving the port")

    def _on_remove_port(self, port: spec.Port | None = None) -> None:
        if port is None:
            return
        guest, host, name = port.guest, port.host, port.name

        def work(progress):
            from .. import ports as ports_mod

            vm_spec = registry.load_spec(self.vm)
            entry = self._find_port(vm_spec.network, guest, host, name)
            if entry is None:
                raise VmhubError("the port list changed; try again")
            vm_spec.network.ports.remove(entry)
            ports_mod.prune_guest_forward(vm_spec.network, guest)
            vm_spec.validate()
            registry.save_spec(vm_spec)
            progress(f"guest {guest} will stop being reachable after the next start")
            return True

        widgets.run_task(self.window, work, label="Removing a port")

    @staticmethod
    def _find_port(net: spec.Network, guest: int, host: int, name: str) -> spec.Port | None:
        """The entry a row was drawn from, matched on what the row showed.

        The table is rebuilt from a poll every few seconds, so the object
        the row holds is a snapshot. Matching on the values the user can
        see means a stale row fails loudly instead of moving the wrong port.
        """
        for port in net.ports:
            if port.guest == guest and port.host == host and port.name == name:
                return port
        return None

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

        widgets.prompt_text(
            self.window,
            "RDP account name",
            "NLA is enabled on the Windows guest, so a real account is required before "
            "the session opens. Enter the account you created during Windows setup - "
            "for a clone it is the same account as the source. Remmina will ask for "
            "the password and can save it.",
            placeholder="e.g. gero",
            accept_label="Connect",
            on_accept=accept,
        )

    def _launch_remmina(self) -> None:
        def work(progress):
            return rdp.launch(self.vm)

        widgets.run_task(
            self.window, work, label=f"Connecting to {self.vm} over RDP"
        )

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
        for _label_text, control in _hw_rows:
            # A DropDown has no "changed" signal at all -- connecting one
            # raises while the pane is being built, which takes the whole
            # window down with it. It notifies on its property instead.
            if isinstance(control, Gtk.DropDown):
                control.connect("notify::selected", self._on_hardware_changed)
            else:
                control.connect("changed", self._on_hardware_changed)
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

        self._add_tab("Hardware", widgets.scroll(box))

    def _build_ports_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        ports_hint = Gtk.Label(
            label="Each forward sends a port on this machine to a port inside the VM. "
            "Web console, SSH and RDP are set up for you; anything else you add is "
            "yours to keep track of. Changes take effect the next time the VM starts."
        )
        ports_hint.add_css_class("hint")
        ports_hint.set_wrap(True)
        ports_hint.set_xalign(0.0)
        box.append(ports_hint)

        self.roles_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.roles_box)
        self.ports_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.ports_box)
        add_button = Gtk.Button(label="Forward another port…")
        add_button.set_halign(Gtk.Align.START)
        add_button.set_tooltip_text(
            "Publish a port of your own -- a database, say -- on a port of your choosing"
        )
        add_button.connect("clicked", self._on_add_port)
        box.append(add_button)

        self._add_tab("Ports", widgets.scroll(box))

    def _build_details_tab(self) -> None:
        # Ports used to share one tab with this, called "Info". Port forwarding is
        # the only part of it you edit, and it is long enough -- a row per role, a
        # row per custom forward, a hint and a button -- that sharing a tab with
        # six read-only fields meant scrolling past all of them to change one
        # number. The name was the tell: "Info" describes neither half.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        self.details_grid = widgets.key_value_grid([])
        box.append(self.details_grid)
        self._add_tab("Details", widgets.scroll(box))

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

        # The whole "Copies of this VM" block moved here from Hardware, not just
        # the Clone button. Moving only the button left a heading on Hardware that
        # existed purely to justify the leftovers, and put the template switch on
        # the other tab from the thing it exists for: marking a template is
        # precisely what makes a clone cheap.
        box.append(widgets.section("Copies of this VM"))
        self.template_hint = Gtk.Label()
        self.template_hint.set_wrap(True)
        self.template_hint.set_xalign(0.0)
        self.template_hint.add_css_class("hint")
        box.append(self.template_hint)

        copies_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.clone_button = Gtk.Button(label="Clone…")
        self.clone_button.set_tooltip_text("Clone this VM (uses its disk as the source)")
        self.clone_button.connect("clicked", self._on_clone)
        self.template_button = Gtk.Button(label="Mark as template")
        self.template_button.connect("clicked", self._on_toggle_template)
        export_button = Gtk.Button(label="Export bundle…")
        export_button.set_tooltip_text(
            "Write a self-contained bundle -- standalone disk, manifest, checksums "
            "-- that another machine can import"
        )
        export_button.connect("clicked", self._on_export)
        copies_row.append(self.clone_button)
        copies_row.append(self.template_button)
        copies_row.append(export_button)
        copies_row.set_halign(Gtk.Align.START)
        box.append(copies_row)

        self.clone_hint = Gtk.Label()
        self.clone_hint.add_css_class("hint")
        self.clone_hint.set_wrap(True)
        self.clone_hint.set_xalign(0.0)
        box.append(self.clone_hint)

        self._add_tab("Snapshots", widgets.scroll(box))

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
        if source != self._log_source:
            return False
        if not self.is_visible():
            # Returning False here removes the timeout, and only building
            # the pane again would start another. Being hidden -- another
            # tab, or a minimised window -- is not a reason to stop
            # watching, so keep the timer and do nothing this time round.
            return True
        if not self._follow:
            return True
        if self._log_reading:
            # A read is still in flight. Every open pane polls every two
            # seconds, so without this a slow `podman logs` would have a
            # second copy started behind it.
            return True
        self._log_reading = True
        vm = self.vm

        def read(_progress):
            # podman logs and podman container exists both spawn a process.
            # Doing that from a GLib timeout is doing it on the main loop,
            # which is what the four-second status refresh was fixed for.
            if not podman.exists(vm):
                return ""
            text = podman.logs(vm, tail=200)
            return "\n".join(
                line for line in text.splitlines() if "\x1b[" not in line
            )

        def done(text: str) -> bool:
            self._log_reading = False
            if vm != self.vm:
                return False
            self._log_buffer.set_text(text[-40000:])
            end = self._log_buffer.get_end_iter()
            self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
            return False

        def failed(_exc: BaseException) -> bool:
            # A VM whose container has gone away is not an error worth a
            # dialog every two seconds.
            self._log_reading = False
            return False

        workers.run_async(read, on_done=done, on_error=failed)
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
        self._clear_box(self.notice_box)
        _replace_grid(self.details_grid, [])
        self._clear_box(self.roles_box)
        self._clear_box(self.ports_box)

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
        # Cloning reads the source disk, which a running VM holds locked.
        self.clone_button.set_sensitive(not running)
        self.clone_button.set_tooltip_text(
            "Stop the VM first — its disk is locked while running"
            if running
            else "Clone this VM (uses its disk as the source)"
        )
        self.guest_button.set_visible(running)
        # Not gated on running any more: a powered-off VM can be
        # snapshotted offline, which is exactly when a clean install is
        # worth protecting.
        self.snapshot_button.set_sensitive(bool(status.has_disk))
        self.snapshot_button.set_tooltip_text(
            "Take an instantaneous copy-on-write snapshot"
            if running
            else "Snapshot the disk as it is, with the VM powered off"
        )

        # Write the VM's current values into the controls only while the
        # user has no unsaved edit in them. They used to be overwritten on
        # every four-second poll regardless, so a half-typed value vanished
        # before Apply could be pressed. The two entries were exempt, but
        # only for as long as they happened to hold focus -- tab away and
        # the edit was discarded.
        if not self._dirty:
            self._loading = True
            try:
                if self.cpu_spin.get_value() != status.cpus:
                    self.cpu_spin.set_value(status.cpus)
                self.ram_entry.set_text(status.ram)
                self.disk_entry.set_text(status.disk_size)
                try:
                    current = registry.load_spec(status.name).shutdown.timeout
                    if abs(self.timeout_spin.get_value() - current) > 0.5:
                        self.timeout_spin.set_value(current)
                except Exception:
                    pass
                if status.disk_type in DISK_TYPES:
                    self.disk_type_drop.set_selected(DISK_TYPES.index(status.disk_type))
            finally:
                self._loading = False

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
        self.rdp_missing_hint.set_visible(not has_rdp)

        # The button never disappears; it explains itself instead. Two reasons
        # it used to vanish without trace: no published RDP port, and Remmina
        # not installed. On a VM with neither, there was nothing on screen to
        # suggest the feature existed.
        remmina_here = rdp.remmina_available()
        self.remmina_button.set_sensitive(has_rdp and remmina_here)
        if not has_rdp:
            self.remmina_button.set_tooltip_text(
                "This VM has no published RDP port. Publish one from the Ports "
                "section and this button will open the guest."
            )
            self.rdp_reason.set_text(
                "RDP is unavailable until a port is published for this VM."
            )
        elif not remmina_here:
            self.remmina_button.set_tooltip_text(
                "Remmina is not installed on this host"
            )
            self.rdp_reason.set_text(
                "Remmina is not installed, so this button has nothing to open."
            )
        else:
            self.remmina_button.set_tooltip_text("Open this VM in Remmina over RDP")
            self.rdp_reason.set_text("")

        # RDP options. The fields show what is in effect, not only what this
        # VM overrides, so an inherited default is visible rather than blank.
        #
        # Two separate things go wrong when a refresh writes into these fields.
        #
        # set_text() emits "changed", which is the signal these handlers save on,
        # so without _rdp_loading every 4-second refresh rewrote every VM's
        # vm.toml, and two of those writes landing at once tore the file into
        # invalid TOML -- which is how a real VM was damaged.
        #
        # And set_text() moves the caret, so writing the value the field already
        # shows is not a no-op: it is a caret jump. Guarding on keyboard focus
        # was not enough, because the caret and the focus disagree exactly when a
        # refresh lands between two keystrokes or after a dialog takes focus. The
        # caret went to the start and the rest of the word was typed in front:
        # "asda" was saved as "456asda", and that mangled name is what reached the
        # Remmina profile. Choosing a folder in Browse went the same way -- the
        # dialog took focus, the poll put the old path back, and it looked like
        # the chooser had selected nothing.
        #
        # So track edits rather than focus. While a field holds an unsaved edit,
        # the refresh does not touch it at all; and it never writes a value the
        # field already shows, because a rewrite with identical text still moves
        # the caret. _sync_entry clears the flag once the field agrees with what
        # is on disk, so the next refresh is free to refresh again.
        self._rdp_loading = True
        try:
            fresh = registry.load_spec(status.name)
            self._sync_entry(self.rdp_user_entry, target.user, fresh.media.rdp_user)
            # Blank means "inherit the default", which is what the label below
            # says. "." is only ever what the user types to opt out.
            self._sync_entry(self.share_entry, "", fresh.media.share_path)
        finally:
            self._rdp_loading = False
        own_share = fresh.media.share_path
        inherited = settings.get("share_path")
        if own_share:
            self.share_source.set_text(f"This VM maps {own_share}.")
        elif inherited:
            self.share_source.set_text(
                f"Inherited from the app-wide default: {inherited}. "
                f"Put '.' here to map nothing for this VM."
            )
        else:
            self.share_source.set_text(
                "No folder is mapped. Set one here, or set share_path in "
                "~/.config/vmhub/settings.toml to use the same folder everywhere."
            )

        self._refresh_iso_rows()
        self._refresh_notices(list(getattr(status, "notices", [])))

        # Same condition the notices use, so the button and the advice can
        # never disagree -- and when there is nothing to do it says so rather
        # than being absent.
        needed = bool(getattr(status, "finish_install_needed", False))
        self.finish_install_button.set_sensitive(needed)
        if needed:
            self.finish_install_button.set_tooltip_text(
                "Detach any install ISO and drop the cached fallback image, so "
                "this VM boots from its disk and nothing else"
            )
        else:
            self.finish_install_button.set_tooltip_text(
                "Nothing to do: no install ISO is attached to this VM and no "
                "fallback image has been cached"
            )

        self._refresh_ports()

        details = [
            ("Container", podman.container_name(status.name)),
            ("Image", registry.load_meta(status.name).image or "-"),
            ("UUID", registry.load_meta(status.name).uuid),
            ("MAC", registry.load_meta(status.name).mac),
            ("Disk bus", status.disk_type),
            ("Boot mode", status.boot_mode),
            ("Backing file", status.backing or "none (self-contained)"),
            ("Guest ports forwarded", ", ".join(str(p) for p in status.guest_ports) or "-"),
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
            self.clone_hint.set_text(
                "Cloning a template is instant: the clone shares this disk until one "
                "of them is deleted, and neither can run while the other holds it."
            )
        elif status.template_source:
            self.clone_hint.set_text(
                f"A linked clone of {status.template_source} shares its disk, which is why "
                "it can be made instantly and why neither can run while the other does."
            )
        else:
            self.clone_hint.set_text(
                "Cloning copies this disk. A plain copy is standalone and costs real "
                "space; marking this VM a template first lets the clone share its disk "
                "instead, which is instant and nearly free."
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
                    f"This VM is a linked clone of {status.template_source}, and shares its "
                    f"disk until that one is deleted — deleting {status.template_source} "
                    "makes this clone standalone rather than breaking it."
                )
            else:
                self.template_hint.set_text(
                    "Mark this VM as a template to make instant, copy-on-write clones from "
                    "it. Stop it first. A linked clone shares this disk, so this VM cannot "
                    "run while one does."
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

    def _on_hardware_changed(self, *_args: object) -> None:
        if not self._loading:
            self._dirty = True

    def _on_apply_resources(self, _button) -> None:
        # Read every control here, on the main loop. The worker runs on a
        # thread, where touching a widget is not safe -- and on a busy main
        # loop it can also read a value the user has already changed.
        cpus = int(self.cpu_spin.get_value())
        ram = self.ram_entry.get_text().strip()
        selected = self.disk_type_drop.get_selected()
        disk_type = DISK_TYPES[selected] if 0 <= selected < len(DISK_TYPES) else ""
        timeout = int(self.timeout_spin.get_value())
        requested = self.disk_entry.get_text().strip()

        try:
            grow_to = spec.parse_size(requested) if requested else 0
        except spec.SpecError as exc:
            self.window.report(f"{requested!r} is not a disk size: {exc}")
            return

        # Growing the disk of a running VM is the one failure that can be
        # reported before anything is touched. Say so and apply the rest,
        # rather than failing the whole edit for one field.
        skipped = ""
        if grow_to:
            try:
                current = registry.load_spec(self.vm).resources.disk_bytes
                if grow_to > current and podman.is_running(self.vm):
                    skipped = (
                        f"; left the disk at {spec.format_size(current)} because "
                        f"{self.vm} is running — stop it and apply again"
                    )
                    grow_to = 0
            except Exception as exc:  # noqa: BLE001
                skipped = f"; left the disk alone: {exc}"
                grow_to = 0

        def work(progress):
            if grow_to:
                # resize() saves the spec itself, so it has to happen before
                # the copy below is loaded, or the size it just wrote gets
                # clobbered by this edit.
                progress(f"growing disk to {requested}…")
                lifecycle.resize(self.vm, requested, progress=progress)
            vm_spec = registry.load_spec(self.vm)
            vm_spec.resources.cpus = cpus
            vm_spec.resources.ram = ram
            if disk_type:
                vm_spec.resources.disk_type = disk_type
            vm_spec.shutdown.timeout = timeout
            vm_spec.validate()
            registry.save_spec(vm_spec)
            return True

        def done(_result) -> bool:
            self._dirty = False
            if skipped:
                self.window.report(f"Applied to {self.vm}{skipped}")
            return False

        widgets.run_task(
            self.window, work, on_done=done, label="Applying resource changes"
        )

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
        can_link = bool(self.status.is_template)

        linked = Gtk.CheckButton(
            label="Linked clone — instant, shares the source's storage"
        )
        full = Gtk.CheckButton(
            label="Full clone — independent copy, takes time and real disk"
        )
        # Mutually exclusive: two plain CheckButtons would let both be ticked.
        full.set_group(linked)
        if can_link:
            linked.set_active(True)
        else:
            full.set_active(True)
            linked.set_sensitive(False)
            linked.set_tooltip_text(
                "A linked clone references the source disk, so the source must be a "
                "template. Mark it with 'Mark as template' to enable this."
            )

        choice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        choice.set_margin_top(8)
        choice.append(linked)
        choice.append(full)
        if not can_link:
            hint = Gtk.Label(
                label="Only full clones are available because this VM is not a template."
            )
            hint.add_css_class("hint")
            hint.set_wrap(True)
            hint.set_xalign(0.0)
            choice.append(hint)

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

        widgets.prompt_text(
            self.window,
            f"Clone {source}",
            "Name the clone.",
            default=f"{source}-copy",
            placeholder="clone name",
            accept_label="Clone",
            extra=choice,
            on_accept=accept,
        )

    def _on_finish_install(self, _button) -> None:
        widgets.run_task(
            self.window,
            lambda progress: lifecycle.finish_install(self.vm, progress=progress),
            label=f"Finishing install of {self.vm}",
            on_done=lambda actions: self.window.report(
                f"{self.vm}: {'; '.join(actions)}"
                if actions
                else f"{self.vm} already boots from its disk"
            ),
        )

    def _on_export(self, _button) -> None:
        widgets.run_task(
            self.window,
            lambda progress: export.export_vm(self.vm, progress=progress),
            label="Exporting bundle",
            on_done=lambda path: self.window.report(f"Exported to {path}"),
        )

    def _delete_dialog(self) -> tuple[Gtk.AlertDialog, bool]:
        """Build the delete confirmation, and say whether it is actually blocked.

        lifecycle.remove() refuses to delete a VM that is still running. The old
        wording promised "This removes the VM, its container and its disk"
        regardless, so deleting a running one looked like it had worked until a
        bare "Operation failed" alert appeared with no way back from it. That is
        what makes a working Delete look broken.

        So say it before the confirmation, and in that case offer no Delete button
        at all -- there is nothing to confirm, so the button would be a lie.

        Returns the dialog and True when the VM is running and cannot be deleted.
        """
        status = self.status
        if status is not None and status.powered:
            return (
                Gtk.AlertDialog(
                    message=f"{self.vm} is running",
                    detail=(
                        "Stop it before deleting. Deleting a VM removes its container "
                        "and its disk, and vmhub will not delete one that is still "
                        "running."
                    ),
                    buttons=["Close"],
                    default_button=0,
                    cancel_button=0,
                ),
                True,
            )
        return (
            Gtk.AlertDialog(
                message=f"Delete {self.vm}?",
                detail="This removes the VM, its container and its disk. "
                "This cannot be undone.",
                buttons=["Cancel", "Delete"],
                cancel_button=0,
                default_button=1,
            ),
            False,
        )

    def _on_delete(self, _button) -> None:
        dialog, blocked = self._delete_dialog()
        if blocked:
            dialog.show(self.window)
            return

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
