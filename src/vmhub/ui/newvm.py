from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import blueprints, lifecycle, spec  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import widgets  # noqa: E402

BLUEPRINT_ROWS = [
    ("(none)", "Start from a blank spec"),
    ("debian13", "Debian 13 — general purpose, good template base"),
    ("alpine", "Alpine Linux — 60 MB, fastest to try out"),
    ("win11", "Windows 11 — IDE during install, then virtio-scsi"),
    ("winserver2022", "Windows Server 2022 — steadier under virtualization"),
]


class NewVmDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(modal=True, transient_for=parent, title="New VM")
        self.parent_window = parent
        self.set_default_size(560, 640)
        self.set_resizable(False)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)

        heading = Gtk.Label(label="Create a virtual machine")
        heading.add_css_class("title-3")
        heading.set_xalign(0.0)
        root.append(heading)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("name, e.g. debian-a")
        self.name_entry.connect("changed", lambda _e: self._validate())
        root.append(widgets.section("Name"))
        root.append(self.name_entry)

        root.append(widgets.section("Blueprint"))
        self.blueprint_drop = Gtk.DropDown.new_from_strings([r[0] for r in BLUEPRINT_ROWS])
        self.blueprint_drop.connect("notify::selected", lambda *_a: self._on_blueprint())
        root.append(self.blueprint_drop)
        self.blueprint_desc = Gtk.Label()
        self.blueprint_desc.add_css_class("hint")
        self.blueprint_desc.set_wrap(True)
        self.blueprint_desc.set_xalign(0.0)
        root.append(self.blueprint_desc)

        root.append(widgets.section("Resources"))
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.cpu_spin = Gtk.SpinButton.new_with_range(1, 128, 1)
        self.cpu_spin.set_value(2)
        self.ram_entry = Gtk.Entry(text="4G")
        self.disk_entry = Gtk.Entry(text="64G")
        for row, (label, control) in enumerate(
            [("vCPUs", self.cpu_spin), ("RAM", self.ram_entry), ("Disk", self.disk_entry)]
        ):
            text = Gtk.Label(label=label)
            text.set_xalign(0.0)
            text.set_size_request(90, -1)
            grid.attach(text, 0, row, 1, 1)
            grid.attach(control, 1, row, 1, 1)
        root.append(grid)

        root.append(widgets.section("Ports"))
        ports_note = Gtk.Label(
            label="Viewer, SSH and RDP ports are assigned automatically from free ports "
            "and stay fixed for the life of the VM."
        )
        ports_note.add_css_class("hint")
        ports_note.set_wrap(True)
        ports_note.set_xalign(0.0)
        root.append(ports_note)

        self.start_check = Gtk.CheckButton(label="Start it immediately after creating")
        self.start_check.set_active(True)
        self.template_check = Gtk.CheckButton(
            label="Mark as template (for instant clones)"
        )
        root.append(self.start_check)
        root.append(self.template_check)

        self.error_label = Gtk.Label()
        self.error_label.add_css_class("errrow")
        self.error_label.set_wrap(True)
        self.error_label.set_xalign(0.0)
        root.append(self.error_label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.create_button = Gtk.Button(label="Create")
        self.create_button.add_css_class("suggested-action")
        self.create_button.set_sensitive(False)
        self.create_button.connect("clicked", self._on_create)
        buttons.append(cancel)
        buttons.append(self.create_button)
        root.append(buttons)

        self.set_child(root)
        self._on_blueprint()

    def _blueprint_name(self) -> str:
        index = self.blueprint_drop.get_selected()
        return BLUEPRINT_ROWS[index][0] if 0 <= index < len(BLUEPRINT_ROWS) else ""

    def _on_blueprint(self) -> None:
        name = self._blueprint_name()
        if not name:
            self.blueprint_desc.set_text("Blank spec. You will need to set a boot image yourself.")
            return
        self.blueprint_desc.set_text(dict((r[0], r[1]) for r in BLUEPRINT_ROWS)[name])
        try:
            base = blueprints.load(name).base_spec("preview")
        except VmhubError as exc:
            self.blueprint_desc.set_text(f"invalid blueprint: {exc}")
            return
        if not self.name_entry.get_text():
            self.name_entry.set_text(f"{name}-1")
        self.cpu_spin.set_value(base.resources.cpus)
        self.ram_entry.set_text(base.resources.ram)
        self.disk_entry.set_text(base.resources.disk)
        self._validate()

    def _validate(self) -> None:
        name = self.name_entry.get_text().strip()
        ok = True
        message = ""
        try:
            spec.validate_name(name)
        except VmhubError as exc:
            ok = False
            message = str(exc)
        for field, label in ((self.ram_entry, "RAM"), (self.disk_entry, "Disk")):
            try:
                spec.parse_size(field.get_text().strip())
            except VmhubError as exc:
                ok = False
                message = f"{label}: {exc}"
        self.create_button.set_sensitive(ok and bool(name))
        self.error_label.set_text(message)

    def _on_create(self, _button) -> None:
        name = self.name_entry.get_text().strip()
        blueprint = self._blueprint_name() or None
        start = self.start_check.get_active()
        as_template = self.template_check.get_active()
        ram = self.ram_entry.get_text().strip()
        disk = self.disk_entry.get_text().strip()
        cpus = int(self.cpu_spin.get_value())
        self.close()

        def work(progress):
            lifecycle.create(
                name,
                blueprint=blueprint,
                cpus=cpus,
                ram=ram,
                disk_size=disk,
                progress=progress,
            )
            if start:
                lifecycle.start(name, progress=progress)
            if as_template:
                lifecycle.stop(name, progress=progress)
                lifecycle.mark_template(name, progress=progress)
            return name

        widgets.run_task(
            self.parent_window,
            work,
            label=f"Creating {name}",
            on_done=lambda vm: self.parent_window.select_vm(vm),
        )
