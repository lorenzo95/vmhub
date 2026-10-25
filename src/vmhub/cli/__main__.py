"""`python3 -m vmhub.cli` -- the entry point the vmctl script runs."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
