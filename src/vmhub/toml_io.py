from __future__ import annotations

from typing import Any

from .errors import SpecError

_BARE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def encode_string(value: str, *, literal: bool = False) -> str:
    if literal and "\n" not in value and "'" not in value:
        return f"'{value}'"
    out = ['"']
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def encode_key(key: str) -> str:
    if key and all(ch in _BARE for ch in key):
        return key
    return encode_string(key)


def encode_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return encode_string(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return "[" + ", ".join(encode_value(item) for item in value) + "]"
    raise SpecError(f"cannot encode value of type {type(value).__name__}: {value!r}")


def _is_table(value: Any) -> bool:
    return isinstance(value, dict)


def _emit(data: dict, prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = [(k, v) for k, v in data.items() if not _is_table(v) and v is not None]
    tables = [(k, v) for k, v in data.items() if _is_table(v)]

    if prefix and (scalars or not tables):
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(encode_key(part) for part in prefix) + "]")
    for key, value in scalars:
        lines.append(f"{encode_key(key)} = {encode_value(value)}")
    for key, value in tables:
        _emit(value, prefix + (key,), lines)


def dumps(data: dict, *, header: str = "") -> str:
    lines: list[str] = []
    if header:
        for line in header.strip().splitlines():
            lines.append(f"# {line}".rstrip())
        lines.append("")
    _emit(data, (), lines)
    body = "\n".join(lines).rstrip() + "\n"
    return body


def loads(text: str) -> dict:
    import tomllib

    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"invalid TOML: {exc}") from exc
