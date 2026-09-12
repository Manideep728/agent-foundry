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
from pathlib import Path

from control_plane import __version__
from control_plane.adapters.fixtures import digest_tree
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import manifest_bytes

PROGRAM = "af"

EXIT_OK = 0
EXIT_USAGE = 2
"""Usage error. Matches the runner's own convention, where 2 is a usage error
and 1 is left unused so a generic failure from a wrapper stays distinguishable.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Run and evaluate AI coding agents against repository tasks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROGRAM} {__version__}"
    )
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    fixture_hash = subcommands.add_parser(
        "fixture-hash",
        help="print the content digest of a fixture directory",
        description=(
            "Print the digest a task file should pin. Producing it with the "
            "same code that verifies it is the point: a digest taken any other "
            "way would surface later as an unexplained FIXTURE_HASH_MISMATCH."
        ),
    )
    fixture_hash.add_argument("directory", type=Path)
    fixture_hash.add_argument(
        "--manifest",
        action="store_true",
        help="also print the canonical manifest, so two fixtures can be diffed",
    )

    return parser


def run_fixture_hash(directory: Path, show_manifest: bool) -> int:
    try:
        digest, entries = digest_tree(directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE

    if show_manifest:
        sys.stdout.write(manifest_bytes(entries).decode("ascii"))
    print(digest)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Return a process exit code. Never calls sys.exit itself, so that tests
    can drive it directly without catching SystemExit."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        print(f"{PROGRAM}: no command given", file=sys.stderr)
        return EXIT_USAGE

    if args.command == "fixture-hash":
        return run_fixture_hash(args.directory, args.manifest)

    # Unreachable: argparse rejects an unknown command before this point.
    print(f"{PROGRAM}: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
