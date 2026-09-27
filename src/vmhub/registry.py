from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import paths, spec
from .errors import AlreadyExists, NotFound


@dataclass
class VmMeta:
    # Never persisted: the directory name is the identity. Kept on the object
    # because the rest of the codebase wants a VM's name at hand.
    name: str = ""
    uuid: str = field(default_factory=spec.new_uuid)
    mac: str = field(default_factory=spec.new_mac)
    created: float = field(default_factory=time.time)
    blueprint: str = ""
    image: str = ""
    is_template: bool = False
    template_source: str | None = None
    dependents: list[str] = field(default_factory=list)
    last_run_state: str = "stopped"
    notes: str = ""
    # Set when meta.json could not be read; never persisted.
    broken: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            k: v for k, v in asdict(self).items() if k not in ("name", "broken")
        }

    @classmethod
    def from_dict(cls, data: dict) -> VmMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _iter_vm_dirs() -> Iterator[Path]:
    root = paths.project_root()
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in {"src", "blueprints", "systemd", "sysvinit", "backups"}:
            continue
        if (entry / paths.META_FILE).is_file():
            yield entry


def all_names() -> list[str]:
    return [d.name for d in _iter_vm_dirs()]


def exists(name: str) -> bool:
    return paths.meta_path(name).is_file()


def load_meta(name: str) -> VmMeta:
    path = paths.meta_path(name)
    if not path.is_file():
        raise NotFound(f"no such VM: {name}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise NotFound(f"{name}: meta.json is unreadable ({exc})") from exc
    meta = VmMeta.from_dict(data)
    meta.name = name
    return meta


def save_meta(meta: VmMeta) -> VmMeta:
    path = paths.meta_path(meta.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta.to_dict(), indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return meta


def delete_meta(name: str) -> None:
    path = paths.meta_path(name)
    if path.is_file():
        path.unlink()


def create_meta(name: str, **kwargs: Any) -> VmMeta:
    if exists(name):
        raise AlreadyExists(f"VM already exists: {name}")
    return save_meta(VmMeta(name=name, **kwargs))


def iter_metas() -> Iterator[VmMeta]:
    """Every VM, named by its directory.

    The filesystem is the only source of names. meta.json deliberately does not
    store one, so renaming or copying a directory cannot desynchronise anything.
    """
    for directory in _iter_vm_dirs():
        try:
            meta = VmMeta.from_dict(json.loads((directory / paths.META_FILE).read_text()))
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            # Never invent a uuid/mac here. The dataclass defaults are freshly
            # random, so a synthesized identity written back would silently
            # replace the real one. Empty values make that fail loudly instead.
            yield VmMeta(
                name=directory.name,
                uuid="",
                mac="",
                notes=f"unreadable meta.json: {exc}",
                broken=True,
            )
            continue
        meta.name = directory.name
        yield meta


def broken_metas() -> list[tuple[str, str]]:
    """(name, reason) for VMs whose meta.json cannot be parsed."""
    return [(m.name, m.notes) for m in iter_metas() if m.broken]


def load_spec(name: str) -> spec.VmSpec:
    """Load a VM's spec, forcing the name to the directory's.

    vm.toml also carries a name, but like meta.json it must not be authoritative:
    a spec that still calls itself its pre-rename self would build container
    paths from a directory that no longer exists.
    """
    return spec.load(paths.spec_path(name), name=name)


def save_spec(vm_spec: spec.VmSpec) -> None:
    spec.save(vm_spec, paths.spec_path(vm_spec.name))


def templates() -> list[VmMeta]:
    return [m for m in iter_metas() if m.is_template]


def dependents_of(template: str) -> list[str]:
    return [m.name for m in iter_metas() if m.template_source == template]


def register_dependent(clone: str, template: str) -> None:
    meta = load_meta(clone)
    meta.template_source = template
    save_meta(meta)
    _sync_dependents(template)


def release_dependent(clone: str) -> str | None:
    meta = load_meta(clone)
    source = meta.template_source
    meta.template_source = None
    save_meta(meta)
    if source:
        _sync_dependents(source)
    return source


def _sync_dependents(template: str) -> None:
    actual = set(dependents_of(template))
    try:
        meta = load_meta(template)
    except NotFound:
        return
    if set(meta.dependents) != actual:
        meta.dependents = sorted(actual)
        save_meta(meta)


def sync_all_dependents() -> None:
    for meta in templates():
        _sync_dependents(meta.name)
