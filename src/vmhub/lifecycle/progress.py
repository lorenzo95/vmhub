"""
progress: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

from typing import Callable


Progress = Callable[[str], None] | None


def _noop(message: str) -> None:
    pass
