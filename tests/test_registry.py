"""The registry: what counts as a VM, and what survives being written.

Two ideas run through this file. First, the filesystem is the only source of
names -- meta.json deliberately does not store one -- so a renamed directory
cannot desynchronise anything. Second, a VM whose meta.json cannot be read is
still a VM that exists; it is yielded with `broken=True` and an empty
identity rather than being skipped or given a fresh random one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vmhub import paths, registry
from vmhub.errors import AlreadyExists, NotFound


def _write_meta(root: Path, name: str, text: str) -> Path:
    vm_dir = root / name
    vm_dir.mkdir(parents=True, exist_ok=True)
    (vm_dir / paths.META_FILE).write_text(text)
    return vm_dir


def test_to_dict_never_persists_the_name_or_the_broken_flag() -> None:
    """Both are derived, not stored: name from the directory, broken from the read."""
    data = registry.VmMeta(name="demo", uuid="u", mac="m", broken=True).to_dict()
    assert "name" not in data
    assert "broken" not in data
    assert data["uuid"] == "u"


def test_from_dict_ignores_keys_it_does_not_know() -> None:
    """An older or newer meta.json must not take the whole listing down."""
    meta = registry.VmMeta.from_dict({"uuid": "u", "who_is_this": "nobody"})
    assert meta.uuid == "u"


def test_a_meta_survives_a_round_trip() -> None:
    original = registry.VmMeta(
        name="demo", uuid="u", mac="m", blueprint="alpine",
        is_template=True, snapshot_power={"before": True},
    )
    back = registry.VmMeta.from_dict(original.to_dict())
    assert back.uuid == "u"
    assert back.mac == "m"
    assert back.blueprint == "alpine"
    assert back.is_template is True
    assert back.snapshot_power == {"before": True}


def test_iter_metas_finds_a_vm_by_its_directory(project_home: Path) -> None:
    _write_meta(project_home, "demo", json.dumps({"uuid": "u", "mac": "m"}))
    assert [m.name for m in registry.iter_metas()] == ["demo"]


def test_a_broken_meta_is_yielded_not_skipped(project_home: Path) -> None:
    """It still exists on disk, so it must still appear -- flagged, not invented."""
    _write_meta(project_home, "broken", "{ not json at all")
    found = [m for m in registry.iter_metas() if m.name == "broken"]
    assert len(found) == 1
    assert found[0].broken is True


def test_a_broken_meta_never_invents_an_identity(project_home: Path) -> None:
    """The dataclass default is a fresh random uuid. Synthesising one here would
    replace the VM's real identity with a stranger's, silently."""
    _write_meta(project_home, "broken", "{ not json at all")
    meta = next(m for m in registry.iter_metas() if m.name == "broken")
    assert meta.uuid == ""
    assert meta.mac == ""


def test_reading_a_broken_meta_does_not_rewrite_it(project_home: Path) -> None:
    """The broken flag is derived, so it must never be written back to disk."""
    original = "{ not json at all"
    vm_dir = _write_meta(project_home, "broken", original)
    next(m for m in registry.iter_metas() if m.name == "broken")
    assert (vm_dir / paths.META_FILE).read_text() == original


def test_broken_metas_lists_the_unreadable_ones(project_home: Path) -> None:
    _write_meta(project_home, "good", json.dumps({"uuid": "u"}))
    _write_meta(project_home, "bad", "{ nope")
    listed = dict(registry.broken_metas())
    assert "bad" in listed
    assert "good" not in listed


@pytest.mark.parametrize("skipped", ["src", "blueprints", "systemd", "sysvinit", "backups"])
def test_project_infrastructure_is_never_mistaken_for_a_vm(
    project_home: Path, skipped: str
) -> None:
    """A directory holding a meta.json is still not a VM if it is the repo."""
    _write_meta(project_home, skipped, json.dumps({"uuid": "x"}))
    assert skipped not in registry.all_names()


def test_a_dot_directory_is_not_a_vm(project_home: Path) -> None:
    _write_meta(project_home, ".staging", json.dumps({"uuid": "x"}))
    assert registry.all_names() == []


def test_a_directory_without_a_meta_json_is_not_a_vm(project_home: Path) -> None:
    (project_home / "leftover").mkdir()
    assert registry.all_names() == []


def test_missing_meta_raises_not_found(project_home: Path) -> None:
    with pytest.raises(NotFound):
        registry.load_meta("nope")


def test_create_then_load_round_trips(project_home: Path) -> None:
    created = registry.create_meta("demo", blueprint="alpine", uuid="u", mac="m")
    assert created.name == "demo"
    assert registry.exists("demo")
    loaded = registry.load_meta("demo")
    assert loaded.uuid == "u"
    assert loaded.blueprint == "alpine"


def test_create_refuses_to_clobber(project_home: Path) -> None:
    registry.create_meta("demo")
    with pytest.raises(AlreadyExists):
        registry.create_meta("demo")


def test_dependents_stay_in_sync_on_both_ends(project_home: Path) -> None:
    """A template's list and each clone's back-pointer must not disagree."""
    registry.create_meta("template", is_template=True)
    registry.create_meta("clone-a")
    registry.create_meta("clone-b")
    registry.register_dependent("clone-a", "template")
    registry.register_dependent("clone-b", "template")

    assert registry.dependents_of("template") == ["clone-a", "clone-b"]
    assert registry.load_meta("template").dependents == ["clone-a", "clone-b"]
    assert registry.load_meta("clone-a").template_source == "template"


def test_releasing_a_dependent_clears_both_ends(project_home: Path) -> None:
    registry.create_meta("template", is_template=True)
    registry.create_meta("clone")
    registry.register_dependent("clone", "template")

    assert registry.release_dependent("clone") == "template"
    assert registry.load_meta("clone").template_source is None
    assert registry.load_meta("template").dependents == []


def test_dependents_are_sorted_not_append_ordered(project_home: Path) -> None:
    """The list is derived from the filesystem, so its order has to be stable."""
    registry.create_meta("template", is_template=True)
    for name in ("zebra", "alpha", "middle"):
        registry.create_meta(name)
        registry.register_dependent(name, "template")
    assert registry.load_meta("template").dependents == ["alpha", "middle", "zebra"]


def test_templates_are_the_ones_flagged_as_such(project_home: Path) -> None:
    registry.create_meta("plain")
    registry.create_meta("template", is_template=True)
    assert [m.name for m in registry.templates()] == ["template"]


def test_a_failed_save_leaves_the_old_meta_and_no_scratch_file(
    project_home: Path, monkeypatch
) -> None:
    """save_meta writes beside the target and renames over it.

    Two things have to hold when the rename fails: the previous meta.json is
    still intact, and no scratch file is left lying about. A stray
    meta.json.tmp is indistinguishable from a real file to anything that
    globs, and it is what the next save would overwrite.
    """
    registry.create_meta("demo", uuid="u")
    original = paths.meta_path("demo").read_text()

    def exploding_replace(*_args, **_kwargs):
        raise OSError("simulated: rename failed")

    with pytest.raises(OSError):
        # monkeypatch.context, not monkeypatch.undo(): undo would also drop the
        # sandbox's VMHUB_HOME and chdir, and the rest of the test would run
        # against the real project root.
        with monkeypatch.context() as patch:
            patch.setattr("os.replace", exploding_replace)
            meta = registry.load_meta("demo")
            meta.uuid = "changed"
            registry.save_meta(meta)

    assert paths.meta_path("demo").read_text() == original
    leftovers = list(paths.vm_dir("demo").glob("*.tmp"))
    assert leftovers == [], f"scratch file left behind: {leftovers}"


def test_two_saves_in_one_process_do_not_share_a_scratch_file(
    project_home: Path, monkeypatch
) -> None:
    """The scratch name has to be unique per call, not per process.

    save_meta runs off the main loop alongside the refresh, so two saves of
    one VM really can overlap. A shared scratch file means they write over each
    other and one's cleanup can unlink the file the other is renaming.
    """
    registry.create_meta("demo", uuid="u")
    seen: list[str] = []
    real_replace = __import__("os").replace

    def recording_replace(src, dst, *args, **kwargs):
        seen.append(str(src))
        return real_replace(src, dst, *args, **kwargs)

    original = paths.meta_path("demo").read_text()
    first = registry.load_meta("demo")
    first.uuid = "one"
    second = registry.load_meta("demo")
    second.uuid = "two"

    import threading

    with monkeypatch.context() as patch:
        patch.setattr("os.replace", recording_replace)
        threads = [
            threading.Thread(target=registry.save_meta, args=(first,)),
            threading.Thread(target=registry.save_meta, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(seen) == 2, f"expected two renames, saw {seen}"
    assert seen[0] != seen[1], f"both saves used the same scratch file: {seen}"
    assert paths.meta_path("demo").read_text() != original
    assert list(paths.vm_dir("demo").glob("*.tmp")) == []