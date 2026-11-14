"""The commands that only read: `fixture-hash`, `show`, `list` and `report`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.storage import (
    database_exists,
    display_name,
    open_repository,
)
from control_plane.adapters.system import SystemClock
from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import manifest_bytes
from control_plane.domain.ids import RunId
from control_plane.interface.cli.common import (
    EXIT_MEASURED,
    EXIT_USAGE,
    PROGRAM,
    exit_code_for,
)
from control_plane.interface.cli.printing import print_record
from control_plane.interface.report import render, unfinished


def run_fixture_hash(directory: Path, show_manifest: bool) -> int:
    try:
        digest, entries = digest_tree(directory)
    except DomainError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_USAGE

    if show_manifest:
        sys.stdout.write(manifest_bytes(entries).decode("ascii"))
    print(digest)
    return EXIT_MEASURED


def show_run(args: argparse.Namespace) -> int:
    if not database_exists(args.db):
        print(f"{PROGRAM}: no database at {display_name(args.db)}", file=sys.stderr)
        return EXIT_USAGE
    with open_repository(args.db, exclusive=False) as repository:
        try:
            record = repository.get(RunId(args.run_id))
        except DomainError as error:
            print(f"{PROGRAM}: {error}", file=sys.stderr)
            return EXIT_USAGE
    if record is None:
        print(f"{PROGRAM}: no run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    print_record(record)
    return exit_code_for(record)


def write_report(args: argparse.Namespace) -> int:
    if not database_exists(args.db):
        print(f"{PROGRAM}: no database at {display_name(args.db)}", file=sys.stderr)
        return EXIT_USAGE

    with open_repository(args.db, exclusive=False) as repository:
        # Every run, not the listing default. A report whose denominator was
        # silently capped at 50 would look complete and be wrong.
        records = repository.list_runs(limit=None)

    if not records:
        # Never EXIT_MEASURED, for the reason `bench` gives: a report of
        # nothing that exits 0 is a green CI job asserting that zero tasks
        # passed.
        print(f"{PROGRAM}: no runs in {display_name(args.db)}", file=sys.stderr)
        return EXIT_USAGE

    text = render(
        records,
        generated_at_unix_ms=SystemClock().now_unix_ms(),
        source=display_name(args.db),
    )

    pending = unfinished(records)
    if pending:
        print(
            f"{PROGRAM}: {len(pending)} run(s) had not finished and are "
            f"excluded; the next af run recovers them",
            file=sys.stderr,
        )

    if str(args.out) == "-":
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return EXIT_MEASURED


def list_runs(args: argparse.Namespace) -> int:
    if not database_exists(args.db):
        print(f"{PROGRAM}: no database at {display_name(args.db)}", file=sys.stderr)
        return EXIT_USAGE
    with open_repository(args.db, exclusive=False) as repository:
        records = repository.list_runs(args.limit)

    if not records:
        print("no runs recorded")
        return EXIT_MEASURED

    for record in records:
        outcome = record.outcome.value if record.outcome else record.run.state.value
        print(
            f"{record.run.run_id}  {record.run.task_id:<24} "
            f"{record.suite.value:<10} {outcome}"
        )
    return EXIT_MEASURED
