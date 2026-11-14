"""`af fetch` and `af import-swebench`: preparing real-repository tasks (D39).

Both reach the network or move gigabytes, so neither ever runs as part of a
measurement. `import-swebench` writes task files from a downloaded dataset;
`fetch` makes the tasks in a directory runnable on this machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from control_plane.adapters.fixtures import digest_tree
from control_plane.adapters.oss_images import (
    FetchFailed,
    build_validation_image,
    ensure_pulled,
    extract_fixture,
    pinned_reference,
)
from control_plane.adapters.swebench import (
    WORKDIR,
    fixture_id_for,
    image_for,
    render_task,
)
from control_plane.adapters.task_toml import base_image, load_task
from control_plane.domain.errors import DomainError
from control_plane.interface.cli.common import (
    EXIT_HARNESS_FAILED,
    EXIT_MEASURED,
    EXIT_USAGE,
    PROGRAM,
)


def fetch_tasks(args: argparse.Namespace) -> int:
    """Pull, extract and build what every real-repository task here needs."""
    paths = sorted(args.directory.glob("*.toml"))
    if not paths:
        print(f"{PROGRAM}: no task files in {args.directory}", file=sys.stderr)
        return EXIT_USAGE
    args.oss_fixtures.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for path in paths:
        try:
            task = load_task(path)
            base = base_image(path)
            if base is None or task.environment is None:
                continue
            destination = args.oss_fixtures / task.fixture_id
            print(f"{PROGRAM}: {task.task_id}: {base}", file=sys.stderr)
            ensure_pulled(base)
            if not _matches(destination, task.fixture_sha256):
                extract_fixture(base, task.environment.workdir, destination)
            if not _matches(destination, task.fixture_sha256):
                digest, _ = digest_tree(destination)
                print(
                    f"{PROGRAM}: {task.task_id}: the image's source tree is {digest}, "
                    f"the task pins {task.fixture_sha256}",
                    file=sys.stderr,
                )
                return EXIT_HARNESS_FAILED
            build_validation_image(base, task.environment.image, args.dockerfile_dir)
        except DomainError as error:
            print(f"{PROGRAM}: {error}", file=sys.stderr)
            return EXIT_USAGE
        except FetchFailed as error:
            print(f"{PROGRAM}: {path.name}: {error}", file=sys.stderr)
            return EXIT_HARNESS_FAILED
        fetched += 1
        print(f"{task.task_id}\t{task.environment.image}")
    print(f"{PROGRAM}: fetched {fetched} real-repository task(s)", file=sys.stderr)
    return EXIT_MEASURED


def _matches(fixture: Path, pinned: str) -> bool:
    if not fixture.is_dir():
        return False
    try:
        digest, _ = digest_tree(fixture)
    except DomainError:
        return False
    return digest == pinned


def import_swebench(args: argparse.Namespace) -> int:
    """Write a task file, its test patch and its reference fix per instance."""
    try:
        rows = _read_rows(args.dataset)
    except (OSError, ValueError) as error:
        print(f"{PROGRAM}: {args.dataset}: {error}", file=sys.stderr)
        return EXIT_USAGE
    missing = [i for i in args.instances if i not in rows]
    if missing:
        print(
            f"{PROGRAM}: not in {args.dataset}: {', '.join(missing)}", file=sys.stderr
        )
        return EXIT_USAGE

    for directory in (args.out / "patches", args.solutions, args.oss_fixtures):
        directory.mkdir(parents=True, exist_ok=True)
    for instance in args.instances:
        try:
            written = _import_one(args, rows[instance])
        except FetchFailed as error:
            print(f"{PROGRAM}: {instance}: {error}", file=sys.stderr)
            return EXIT_HARNESS_FAILED
        except (DomainError, ValueError) as error:
            print(f"{PROGRAM}: {instance}: {error}", file=sys.stderr)
            return EXIT_USAGE
        print(f"{instance}\t{written}")
    return EXIT_MEASURED


def _read_rows(dataset: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[str(row["instance_id"])] = row
    return rows


def _import_one(args: argparse.Namespace, row: dict[str, Any]) -> Path:
    instance = str(row["instance_id"])
    fixture_id = fixture_id_for(instance)
    image = image_for(instance)
    print(f"{PROGRAM}: {instance}: pulling {image}", file=sys.stderr)
    ensure_pulled(image)
    pinned = pinned_reference(image)
    destination = args.oss_fixtures / fixture_id
    extract_fixture(pinned, WORKDIR, destination)
    digest, _ = digest_tree(destination)

    test_patch = str(row["test_patch"])
    patch_name = f"patches/{fixture_id}.test.patch"
    (args.out / patch_name).write_text(test_patch, encoding="utf-8")
    (args.solutions / f"{fixture_id}.patch").write_text(
        str(row["patch"]), encoding="utf-8"
    )
    task_file: Path = args.out / f"{fixture_id}.toml"
    task_file.write_text(
        render_task(
            row,
            fixture_sha256=digest,
            base_image=pinned,
            test_patch_file=patch_name,
            test_patch_sha256=hashlib.sha256(test_patch.encode("utf-8")).hexdigest(),
        ),
        encoding="utf-8",
    )
    load_task(task_file)  # written by this build, so it must load in this build
    return task_file
