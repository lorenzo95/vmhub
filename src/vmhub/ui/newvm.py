from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import blueprints, lifecycle, provision, registry, seed, spec  # noqa: E402
from ..errors import VmhubError  # noqa: E402
from . import widgets  # noqa: E402

NONE_BLUEPRINT = "(none)"

def _blueprint_rows() -> list[tuple[str, str]]:
    """Build the dropdown from blueprints/ when the dialog opens, so a new
    blueprint file shows up without a code change. A file that fails to
    load still gets a row; selecting it shows the error in the hint."""
    rows = [(NONE_BLUEPRINT, "Start from a blank spec")]
    for name in blueprints.names():
        try:
            bp = blueprints.load(name)
        except VmhubError as exc:
            rows.append((name, f"invalid blueprint: {exc}"))
            continue
        rows.append((name, bp.description or bp.title or name))
    return rows


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
        self._rows = _blueprint_rows()
        self.blueprint_drop = Gtk.DropDown.new_from_strings([r[0] for r in self._rows])
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
        # Every field is validated as it is typed. These two had no handler
        # at all, so "4G" -> "banana" left Create enabled and the error
        # label empty, and the failure only surfaced in the worker with
        # the dialog already closed.
        for field in (self.ram_entry, self.disk_entry):
            field.connect("changed", lambda _e: self._validate())
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
        name = self._rows[index][0] if 0 <= index < len(self._rows) else ""
        # "(none)" is a label for "no blueprint", not the name of one. It
        # was passed through as a name, so the default row of the default
        # dialog said "no such blueprint: (none)" and Create could only fail.
        return "" if name == NONE_BLUEPRINT else name

    def _on_blueprint(self) -> None:
        name = self._blueprint_name()
        if not name:
            self.blueprint_desc.set_text("Blank spec. You will need to set a boot image yourself.")
            return
        self.blueprint_desc.set_text(dict(self._rows)[name])
        try:
            bp = blueprints.load(name)
            base = bp.base_spec("preview")
        except VmhubError as exc:
            self.blueprint_desc.set_text(f"invalid blueprint: {exc}")
            return
        if bp.image:
            # A cloud blueprint is provisioned, not installed: say so here,
            # because Create will download an image and seed cloud-init
            # instead of leaving an empty disk for an installer.
            self.blueprint_desc.set_text(
                dict(self._rows)[name]
                + " Provisioned from a cloud image with cloud-init -- no installer."
                " Your ~/.ssh public keys are authorised for the guest account."
            )
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
            if name and registry.exists(name):
                # Create used to find this out in the worker, after the
                # dialog had closed and the typed-in values were gone.
                ok = False
                message = f"a VM called {name!r} already exists"
        except VmhubError as exc:
            ok = False
            message = str(exc)
        for field, label in ((self.ram_entry, "RAM"), (self.disk_entry, "Disk")):
            if not ok:
                break
            try:
                spec.parse_size(field.get_text().strip())
            except VmhubError as exc:
                ok = False
                message = f"{label}: {exc}"
        self.create_button.set_sensitive(ok and bool(name))
        self.error_label.set_text(message)
        return ok and bool(name)

    def _on_create(self, _button) -> None:
        # Check once more before closing: a handler can be invoked without
        # the fields having been typed through, and closing first throws the
        # dialog away while the message explaining why goes to the window
        # behind it.
        if not self._validate():
            return
        name = self.name_entry.get_text().strip()
        blueprint = self._blueprint_name() or None
        image_key = blueprints.load(blueprint).image if blueprint else ""
        start = self.start_check.get_active()
        as_template = self.template_check.get_active()
        ram = self.ram_entry.get_text().strip()
        disk = self.disk_entry.get_text().strip()
        cpus = int(self.cpu_spin.get_value())
        self.close()

        def work(progress):
            if image_key:
                # The provision path, same as `vmctl provision`: fetch the
                # cloud image, convert it into the VM's disk, attach the
                # cloud-init seed, start, wait. lifecycle.create alone
                # would leave an empty disk with nothing to boot.
                keys = seed.discover_ssh_keys()
                if not keys:
                    raise VmhubError(
                        "no SSH public keys found in ~/.ssh: a cloud-image VM "
                        "needs one to let you in. Create one with ssh-keygen, "
                        "then try again."
                    )
                user_data = seed.default_user_data(hostname=name, ssh_keys=keys)
                provision.provision(
                    name,
                    blueprint=blueprint,
                    image=image_key,
                    user_data=user_data,
                    user=seed.DEFAULT_USER,
                    cpus=cpus,
                    ram=ram,
                    disk=disk,
                    progress=progress,
                )
                # Provisioning boots the VM so cloud-init can run; honour
                # an unchecked "start immediately" by stopping it after.
                if not start:
                    lifecycle.stop(name, progress=progress)
            else:
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
            label=f"Provisioning {name}" if image_key else f"Creating {name}",
            on_done=lambda vm: self.parent_window.select_vm(vm),
        )
