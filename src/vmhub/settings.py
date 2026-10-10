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
    "image_tag": (
        "QEMU container tag new VMs get, e.g. 7.50 or latest; empty uses the "
        "version vmhub ships pinned to"
    ),
    "rdp_resolution": (
        "Resolution RDP sessions ask for, as WIDTHxHEIGHT, e.g. 2560x1440; "
        "empty uses the one vmhub ships with"
    ),
}

DEFAULTS: dict[str, str] = {key: "" for key in FIELDS}


class SettingError(ValueError):
    """A setting was given a value that cannot mean anything."""


def _no_problem(value: str) -> str | None:
    return None


def _check(key: str, value: str) -> None:
    """Refuse values that would only fail later, somewhere less obvious.

    A tag goes straight into `repo:tag` and from there into podman; a
    resolution goes into the RDP connection and the window it opens. Typing
    "7.5 0" or "big" into preferences would otherwise become an opaque
    failure at the first connection or pull -- long after the box that was
    typed into.
    """
    problem = _VALIDATORS.get(key, _no_problem)(value)
    if problem:
        raise SettingError(problem)


def image_tag_problem(tag: str) -> str | None:
    """Why this tag cannot be used, or None if it can. Empty is fine."""
    if not tag:
        return None
    if any(ch.isspace() for ch in tag):
        return f"image tag {tag!r} contains whitespace"
    for ch in ":/@":
        if ch in tag:
            return (
                f"image tag {tag!r} contains {ch!r} -- that is part of the "
                "repository or a digest, not the tag. Use `vmctl config set "
                "image_tag <tag>` for just the version, e.g. 7.50 or latest"
            )
    return None


# A window bigger than this is not a screen anyone has, and RDP implementations
# start refusing or silently clamping well before it.
MAX_RESOLUTION_SIDE = 7680


def resolution_problem(resolution: str) -> str | None:
    """Why this resolution cannot be used, or None if it can. Empty is fine."""
    if not resolution:
        return None
    width, sep, height = resolution.partition("x")
    if not sep:
        return (
            f"resolution {resolution!r} is missing the x -- it is WIDTHxHEIGHT, "
            "e.g. 1920x1080 or 2560x1440"
        )
    for name, value in (("width", width), ("height", height)):
        if not value.isdigit():
            return (
                f"resolution {resolution!r} has a non-numeric {name}; it is "
                "WIDTHxHEIGHT, e.g. 1920x1080"
            )
        if not 1 <= int(value) <= MAX_RESOLUTION_SIDE:
            return (
                f"resolution {resolution!r} has an impossible {name} -- between "
                f"1 and {MAX_RESOLUTION_SIDE}"
            )
    return None


# Per-key, because a free-text field has nothing to check against and a field
# that ends up in a command line or a window size very much does. Built after
# both functions, since it names them at module level.
_VALIDATORS = {"image_tag": image_tag_problem, "rdp_resolution": resolution_problem}


def _image_tag_fallback() -> str:
    # Imported inside the function on purpose: spec imports this module, so a
    # module-level import here would be circular. Called only when rendering,
    # never at import time, so the cycle is never entered.
    from . import spec

    # DEFAULT_TAG, not default_tag(): this is what the field falls back to
    # when nothing is set. default_tag() already folds in the preference, so
    # using it here would make fallback() and effective() the same function
    # under two names, and the name would be a lie.
    return spec.DEFAULT_TAG


# What a field resolves to when nothing is set, for anything that displays it.
# Only image_tag has one, and that is the point of keeping it here rather than
# printing "(unset)" like the other two: "(unset)" is true and useless for a
# field whose answer is a real value. It answered "is a value configured?" when
# the question was "what will a new VM pull?", and nothing on the screen said
# 7.50 -- which is the number the person actually needed.
def _rdp_resolution_fallback() -> str:
    from . import rdp  # see _image_tag_fallback: circular if imported at module level

    return rdp.shipped_resolution()


_FALLBACKS: dict[str, object] = {
    "image_tag": _image_tag_fallback,
    "rdp_resolution": _rdp_resolution_fallback,
}


def fallback(key: str) -> str:
    """What this field will be when it is unset, or "" if it has no default.

    Not the same question as load(): a field with no fallback really does end
    up empty, so "(unset)" remains the honest thing to print for those.
    """
    probe = _FALLBACKS.get(key)
    return probe() if callable(probe) else ""


def effective(key: str) -> str:
    """The value that will actually be used, set or not."""
    return load().get(key, "").strip() or fallback(key)


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
            # validate() rejects the same values save() refuses to store, so
            # this is the hand-edited case: someone opened settings.toml in an
            # editor and typed a whole image reference into the tag. Dropping
            # it here degrades to the shipped default, which is the same
            # "nothing configured" answer every other unreadable value gets,
            # rather than handing a broken reference to podman later.
            try:
                _check(key, found)
            except SettingError:
                continue
            values[key] = found
    return values


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(values: dict[str, str]) -> Path:
    """Write the defaults back, keeping the file self-documenting.

    Validates before writing anything, so a rejected value leaves the file
    that is already there rather than a half-updated one.
    """
    for key in FIELDS:
        _check(key, values.get(key, DEFAULTS[key]))
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