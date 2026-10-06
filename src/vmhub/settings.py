"""Defaults that belong to the installation, not to any one VM.

A vm.toml records what is true of that machine. These are the choices a user
makes once for the whole install: the account they sign in to Windows with,
and the folder they want mapped into every RDP session. They live outside the
project checkout so they survive a re-clone, and outside every vm.toml so they
do not have to be repeated per machine.

There are no default ports here, deliberately. A published host port can
belong to exactly one VM, so a "default port" could apply to one machine ever
and would then collide. Allocation has to stay per-VM.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV_CONFIG = "VMHUB_CONFIG"

# Key -> what it does. The GUI uses these to label the fields, and `vmctl
# config` prints them so the file explains itself.
FIELDS: dict[str, str] = {
    "rdp_username": (
        "Account used for RDP and Remmina when a VM sets none of its own"
    ),
    "share_path": (
        "Host folder mapped into every RDP session; empty maps nothing"
    ),
}

DEFAULTS: dict[str, str] = {key: "" for key in FIELDS}


def config_path() -> Path:
    """Where the defaults live. VMHUB_CONFIG wins, then XDG_CONFIG_HOME."""
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "vmhub" / "settings.toml"


def load() -> dict[str, str]:
    """The defaults, with anything unset or unreadable left empty.

    A settings file that cannot be parsed must not stop the app from starting.
    An unreadable default is not worth losing a list of VMs over, so this
    degrades to "nothing configured" rather than raising.
    """
    values = dict(DEFAULTS)
    try:
        raw = tomllib.loads(config_path().read_text())
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return values
    for key in FIELDS:
        found = raw.get(key)
        if isinstance(found, str):
            values[key] = found
    return values


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(values: dict[str, str]) -> Path:
    """Write the defaults back, keeping the file self-documenting."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# vmhub defaults.",
        "# These are yours, not the project's: this file sits outside the checkout",
        "# so it survives a re-clone. A VM's own vm.toml overrides anything here.",
        "",
    ]
    for key, description in FIELDS.items():
        lines.append(f"# {description}")
        lines.append(f"{key} = {_quote(values.get(key, DEFAULTS[key]))}")
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def get(key: str) -> str:
    if key not in FIELDS:
        raise KeyError(f"unknown setting: {key}")
    return load()[key]


def set_value(key: str, value: str) -> Path:
    if key not in FIELDS:
        raise KeyError(f"unknown setting: {key}")
    values = load()
    values[key] = value
    return save(values)