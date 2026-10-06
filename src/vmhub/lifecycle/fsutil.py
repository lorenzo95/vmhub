"""
fsutil: part of the lifecycle API.

Moved verbatim out of the old single-file lifecycle.py; only the
grouping and the import list are new.
"""

from __future__ import annotations

from pathlib import Path

from .progress import Progress, _noop


def os_replace(src: Path, dst: Path) -> None:
    import os

    os.replace(src, dst)
