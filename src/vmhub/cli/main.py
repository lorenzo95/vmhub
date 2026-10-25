"""The entry point: parse the arguments, run the command, report what went wrong."""

from __future__ import annotations

import sys

from ..errors import VmhubError
from .output import Out
from .parser import build_parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out()
    try:
        return args.func(args, out) or 0
    except KeyboardInterrupt:
        print()
        return 130
    except VmhubError as exc:
        print(out.bad(f"error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
