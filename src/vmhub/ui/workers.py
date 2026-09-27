from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from gi.repository import GLib

Progress = Callable[[str], None]


def run_async(
    work: Callable[[Progress], Any],
    *,
    on_progress: Callable[[str], None] | None = None,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    def emit_progress(message: str) -> None:
        if on_progress is not None:
            GLib.idle_add(on_progress, message)

    def worker() -> None:
        try:
            result = work(emit_progress)
        except BaseException as exc:  # noqa: BLE001
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            else:
                GLib.idle_add(_default_error, exc)
            return
        if on_done is not None:
            GLib.idle_add(on_done, result)

    threading.Thread(target=worker, name="vmhub-task", daemon=True).start()


def _default_error(exc: BaseException) -> bool:
    import sys

    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    print(f"vmhub: {exc}", file=sys.stderr)
    return False


def describe(exc: BaseException) -> str:
    if isinstance(exc, BaseException) and exc.__traceback__ is not None:
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            last = frames[-1]
            return f"{exc}  ({last.filename.split('/')[-1]}:{last.lineno})"
    return str(exc)
