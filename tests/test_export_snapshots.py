"""An exported bundle carries the VM's snapshots, not just its current state.

Export used to run `qemu-img convert` on every disk, chained or not.
That flattens a backing chain, which a standalone disk does not need, and
it also writes the image's current state as a brand new image -- so the
internal snapshots were dropped on the way out. The manifest then read
its snapshot list off that converted image, so it reported the empty
list it had just created rather than the snapshots the VM actually had.

A backup you cannot revert to is not a backup, so a standalone disk is
now copied, which keeps the snapshots, while a linked clone still goes
through convert because a copy would leave a backing reference to a
path that is not in the bundle.
"""

from __future__ import annotations

import pytest

from vmhub import disk, export, lifecycle, paths


def _make_vm(root, name: str, *, snapshot: str | None = None) -> None:
    """A VM with a small real disk, optionally carrying one snapshot."""
    vm_dir = root / name
    (vm_dir / paths.BLUEPRINTS_DIR).mkdir(parents=True, exist_ok=True)
    (vm_dir / "vmctl").write_text("#!/bin/sh\n")
    (vm_dir / paths.SPEC_FILE).write_text(
        '[resources]\ncpus = 2\nram = "4G"\ndisk = "1G"\n\n'
        '[network]\nmode = "user"\nbind = "127.0.0.1"\n'
    )
    (vm_dir / paths.META_FILE).write_text(
        '{"uuid": "u", "mac": "m", "created": "2026-01-01"}\n'
    )
    image = paths.disk_path(name)
    image.parent.mkdir(parents=True, exist_ok=True)
    disk.create(image, "1G")
    if snapshot:
        disk.create_snapshot(image, snapshot)


def _snapshots(name: str) -> list[str]:
    return disk.snapshot_names(paths.disk_path(name))


def test_a_standalone_disk_is_copied_not_converted(project_home) -> None:
    """A convert is a rewrite: it costs a full read and write of the image
    to produce the same bytes for a disk that has no chain to resolve."""
    _make_vm(project_home, "plain")
    assert disk.backing_chain(paths.disk_path("plain")) == [
        str(paths.disk_path("plain"))
    ]


def test_an_exported_bundle_carries_the_snapshot(project_home, tmp_path) -> None:
    """The whole point: the snapshot is in the bundle afterwards."""
    _make_vm(project_home, "snappy", snapshot="before-upgrade")
    assert _snapshots("snappy") == ["before-upgrade"]

    bundle = export.export_vm("snappy", dest_dir=tmp_path / "out")
    manifest = export.read_manifest(bundle)
    assert manifest["disk"]["snapshots"] == ["before-upgrade"], manifest["disk"]


def test_an_imported_bundle_can_still_revert(project_home, tmp_path) -> None:
    """Not just recorded in the manifest -- actually usable after import."""
    _make_vm(project_home, "snappy", snapshot="before-upgrade")
    bundle = export.export_vm("snappy", dest_dir=tmp_path / "out")
    export.import_bundle(bundle, name="restored")

    assert _snapshots("restored") == ["before-upgrade"]
    lifecycle.revert("restored", "before-upgrade")
    assert _snapshots("restored") == ["before-upgrade"]


def test_a_vm_without_snapshots_still_exports(project_home, tmp_path) -> None:
    """The copy path must not break the ordinary case."""
    _make_vm(project_home, "plain")
    bundle = export.export_vm("plain", dest_dir=tmp_path / "out")
    manifest = export.read_manifest(bundle)
    assert manifest["disk"]["snapshots"] == []
    assert manifest["disk"]["backing"] is None


def test_a_linked_clone_is_still_flattened(project_home, tmp_path) -> None:
    """A copy would be faster here too, and wrong: the overlay names a
    backing file that is not in the bundle, so the result could not be
    imported anywhere."""
    _make_vm(project_home, "source", snapshot="base")
    # A linked clone references its source's disk, so the source has to be a
    # template -- which is exactly the case that produces a backing chain.
    lifecycle.mark_template("source", progress=lambda _m: None)
    lifecycle.clone("clone", "source", mode="linked", progress=lambda _m: None)

    assert len(disk.backing_chain(paths.disk_path("clone"))) > 1, "not a chain"

    bundle = export.export_vm("clone", dest_dir=tmp_path / "out")
    manifest = export.read_manifest(bundle)
    assert manifest["disk"]["backing"] is None, manifest["disk"]
    assert manifest["resolved_backing_chain"] is True, manifest

    export.import_bundle(bundle, name="restored-clone")
    assert len(disk.backing_chain(paths.disk_path("restored-clone"))) == 1, (
        "the imported clone still points at a backing file"
    )


def test_copy_sparse_refuses_to_overwrite(project_home, tmp_path) -> None:
    _make_vm(project_home, "plain")
    destination = tmp_path / "already-there.qcow2"
    destination.write_bytes(b"do not clobber")
    with pytest.raises(disk.DiskError):
        disk.copy_sparse(paths.disk_path("plain"), destination)
    assert destination.read_bytes() == b"do not clobber"


def test_copy_sparse_does_not_explode_a_sparse_image(project_home, tmp_path) -> None:
    """A plain non-sparse copy of a 1G empty image writes 1G. This one
    must stay small, or every export quietly doubles in size."""
    _make_vm(project_home, "plain")
    destination = tmp_path / "copy.qcow2"
    disk.copy_sparse(paths.disk_path("plain"), destination)
    assert destination.stat().st_size < 64 * 1024 * 1024, (
        f"copied {destination.stat().st_size} bytes for a 1 GiB empty disk"
    )