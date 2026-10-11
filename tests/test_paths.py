"""Where vmhub decides things live.

`project_root()` is the one function every other path helper defers to, so
these tests are really about one question: given a directory, how does vmhub
decide that it is the project root, and how sure is it?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vmhub import paths
from vmhub.errors import ProjectRootError


def _make_root(base: Path) -> Path:
    """A directory carrying everything `_is_project_root` requires."""
    root = base / "vmhub"
    (root / "blueprints").mkdir(parents=True, exist_ok=True)
    (root / "vmctl").write_text("#!/bin/sh\n")
    return root


def test_env_home_wins_over_discovery(project_home: Path, tmp_path: Path) -> None:
    """VMHUB_HOME is authoritative, even from a cwd that looks like a checkout."""
    other = _make_root(tmp_path / "elsewhere")
    assert paths.project_root() == project_home.resolve()
    assert other != project_home


def test_markers_are_both_required(tmp_path: Path) -> None:
    """blueprints/ alone is not a project root, and neither is vmctl alone."""
    blueprints_only = tmp_path / "blueprints-only"
    (blueprints_only / "blueprints").mkdir(parents=True)
    vmctl_only = tmp_path / "vmctl-only"
    vmctl_only.mkdir(parents=True)
    (vmctl_only / "vmctl").write_text("#!/bin/sh\n")

    assert not paths._is_project_root(blueprints_only)
    assert not paths._is_project_root(vmctl_only)
    assert paths._is_project_root(_make_root(tmp_path / "both"))


def test_a_directory_without_markers_is_refused(tmp_path: Path, monkeypatch) -> None:
    """The failure names the directory it rejected, not the filesystem root."""
    empty = tmp_path / "not-a-project"
    empty.mkdir()
    monkeypatch.setenv(paths.ENV_HOME, str(empty))
    with pytest.raises(ProjectRootError) as excinfo:
        paths.project_root()
    assert str(empty) in str(excinfo.value)


def test_discovery_walks_up_from_a_nested_directory(tmp_path: Path, monkeypatch) -> None:
    """Running from src/vmhub/ui/ still finds the root above it."""
    root = _make_root(tmp_path)
    nested = root / "src" / "vmhub" / "ui"
    nested.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.chdir(nested)
    assert paths.project_root() == root.resolve()


def test_vm_paths_are_all_under_the_root(project_home: Path) -> None:
    """Every per-VM path is a child of the root, built from the VM's name."""
    root = project_home.resolve()
    assert paths.vm_dir("demo").resolve() == (root / "demo").resolve()
    assert paths.spec_path("demo").resolve() == (root / "demo" / "vm.toml").resolve()
    assert paths.meta_path("demo").resolve() == (root / "demo" / "meta.json").resolve()
    assert paths.storage_dir("demo").resolve() == (root / "demo" / "storage").resolve()
    assert paths.disk_dir("demo").resolve() == (root / "demo" / "disk").resolve()
    assert paths.disk_path("demo").resolve() == (
        root / "demo" / "disk" / "data.qcow2"
    ).resolve()


def test_the_sandbox_keeps_every_path_inside_the_temp_tree(
    project_home: Path, tmp_path: Path
) -> None:
    """The reason the fixture exists: a test cannot reach the real checkout.

    These helpers resolve to the live project root by default, so a test that
    reached one would act on whatever is really there rather than fail.
    """
    sandbox = str(tmp_path.resolve())
    for path in (
        paths.vm_dir("demo"),
        paths.spec_path("demo"),
        paths.meta_path("demo"),
        paths.storage_dir("demo"),
        paths.disk_path("demo"),
    ):
        assert str(path.resolve()).startswith(sandbox), path


def test_paths_agree_on_which_vm_they_name(project_home: Path) -> None:
    """Two VMs never share a directory, however similar their names."""
    assert paths.vm_dir("alpha") != paths.vm_dir("beta")
    assert paths.vm_dir("winni").resolve().parent == project_home.resolve()


def test_blueprints_dir_tracks_the_root(project_home: Path) -> None:
    assert paths.blueprints_dir() == project_home / paths.BLUEPRINTS_DIR


def test_backup_dir_is_created_on_demand(project_home: Path) -> None:
    """backup_dir() has a side effect by design: callers write into it."""
    backups = paths.backup_dir()
    assert backups.is_dir()
    assert backups == project_home / "backups"


def test_qmp_socket_lives_in_storage(project_home: Path) -> None:
    """The socket has to be where the container expects to bind it."""
    assert paths.qmp_path("demo").resolve() == (
        paths.storage_dir("demo").resolve() / "qmp.sock"
    ).resolve()


def test_the_fallback_prefers_the_directory_a_clone_creates() -> None:
    """`git clone` names the directory after the repository, so the documented
    fallback has to be ~/vmhub. The README said ~/vms throughout while the
    code and every clone said otherwise."""
    assert paths.DEFAULT_HOME == "~/vmhub", paths.DEFAULT_HOME


def _make_convention_root(base: Path) -> Path:
    """A checkout at an exact name, unlike _make_root which always appends
    "vmhub" -- here the directory name is the thing under test."""
    base.mkdir(parents=True, exist_ok=True)
    (base / paths.BLUEPRINTS_DIR).mkdir(exist_ok=True)
    (base / "vmctl").write_text("#!/bin/sh\n")
    return base


def _root_found_from_nowhere(monkeypatch, tmp_path, *conventions: str) -> Path:
    """Run discovery from a directory with no checkout above it, with the
    home-directory conventions pointing into a temp tree.

    monkeypatch.context(), not plain monkeypatch: the sandbox guard re-resolves
    the project root at teardown, and a patch still in place would make it
    resolve to the temp tree and fail an unrelated assertion.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in conventions:
        _make_convention_root(home / name)

    with monkeypatch.context() as patch:
        patch.delenv(paths.ENV_HOME, raising=False)
        patch.setattr(paths, "_expand_home", lambda c: home / Path(c).name)
        patch.chdir(tmp_path)      # somewhere with no project root above it
        return paths.project_root().resolve()


def test_a_checkout_at_the_documented_location_is_found_when_nothing_else_says_where(
    monkeypatch, tmp_path
) -> None:
    """The point of the fallback: run vmctl from an unrelated directory and
    still land on the checkout."""
    assert _root_found_from_nowhere(monkeypatch, tmp_path, "vmhub") == (
        tmp_path / "home" / "vmhub"
    )


def test_a_checkout_at_the_old_location_is_still_found(monkeypatch, tmp_path) -> None:
    """Someone who set their install up against the old README is not left
    broken: the fallback tries the legacy name too, so ~/vms still resolves
    when nothing else identifies a checkout."""
    assert _root_found_from_nowhere(monkeypatch, tmp_path, "vms") == (
        tmp_path / "home" / "vms"
    )


def test_vmhub_is_tried_before_the_legacy_name(monkeypatch, tmp_path) -> None:
    """With both present the current convention wins -- otherwise an old ~/vms
    would quietly shadow the checkout someone just cloned."""
    assert _root_found_from_nowhere(monkeypatch, tmp_path, "vmhub", "vms") == (
        tmp_path / "home" / "vmhub"
    )


def test_the_fallback_resolves_to_nothing_without_a_checkout(
    monkeypatch, tmp_path
) -> None:
    """And it is still only a fallback: with no checkout at either
    convention, the answer is where the user actually was."""
    from vmhub.errors import ProjectRootError

    with pytest.raises(ProjectRootError):
        _root_found_from_nowhere(monkeypatch, tmp_path)
