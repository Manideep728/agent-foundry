"""Command line entry point.

Subcommands are added here as the behaviour behind them lands. A subcommand
that cannot do its job exits non-zero with a message on stderr rather than
printing a plausible-looking placeholder result -- a harness that reports
fabricated outcomes is worse than one that reports none.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from control_plane import __version__

PROGRAM = "af"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Run and evaluate AI coding agents against repository tasks.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROGRAM} {__version__}",
    )
    # `required=False` so that a bare `af` falls through to the usage path
    # below and exits 2, rather than argparse raising its own error.
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return a process exit code. Never calls sys.exit itself, so that tests
    can drive it directly without catching SystemExit."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        print(f"{PROGRAM}: no command given", file=sys.stderr)
        return 2

    # Unreachable while no subcommands are registered; argparse rejects any
    # unknown command before this point.
    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
