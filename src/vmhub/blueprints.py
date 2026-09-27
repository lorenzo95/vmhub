from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, spec, toml_io
from .errors import NotFound, SpecError

RESERVED = {"name", "title", "description", "icon", "os", "spec", "notes", "ssh_user", "rdp_user"}


@dataclass
class Blueprint:
    name: str
    title: str = ""
    description: str = ""
    icon: str = "computer-symbolic"
    os: str = "linux"
    notes: str = ""
    ssh_user: str = "user"
    rdp_user: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)

    def base_spec(self, name: str) -> spec.VmSpec:
        vm_spec = spec.spec_from_dict({"name": name, **self.overrides})
        vm_spec.blueprint = self.name
        return vm_spec.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title or self.name,
            "description": self.description,
            "icon": self.icon,
            "os": self.os,
            "notes": self.notes,
            "ssh_user": self.ssh_user,
            "rdp_user": self.rdp_user,
            "spec": self.overrides,
        }


def _from_dict(name: str, data: dict) -> Blueprint:
    unknown = set(data) - RESERVED
    if unknown:
        raise SpecError(f"blueprint {name}: unknown keys {sorted(unknown)}")
    return Blueprint(
        name=name,
        title=data.get("title", ""),
        description=data.get("description", ""),
        icon=data.get("icon", "computer-symbolic"),
        os=data.get("os", "linux"),
        notes=data.get("notes", ""),
        ssh_user=data.get("ssh_user", "user"),
        rdp_user=data.get("rdp_user", ""),
        overrides=data.get("spec", {}) or {},
    )


def load(name: str) -> Blueprint:
    path = paths.blueprints_dir() / f"{name}.toml"
    if not path.is_file():
        raise NotFound(f"no such blueprint: {name} (looked in {path})")
    return _from_dict(name, toml_io.loads(path.read_text()))


def names() -> list[str]:
    directory = paths.blueprints_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.toml"))


def iter_all() -> list[Blueprint]:
    return [load(name) for name in names()]


def save(blueprint: Blueprint) -> Path:
    path = paths.blueprints_dir() / f"{blueprint.name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        toml_io.dumps(
            blueprint.to_dict(),
            header=f"vmhub blueprint: {blueprint.title or blueprint.name}",
        )
    )
    return path
