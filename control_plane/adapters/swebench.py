"""Turning one SWE-bench instance into a task file (D39).

Pure: a dataset row and the values `af fetch` measured go in, text comes out.
The row is read from a JSONL file the operator downloaded; nothing here
reaches the network.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence

INTERPRETER = "/opt/miniconda3/envs/testbed/bin/python"
"""The environment every SWE-bench instance image installs the repository into."""

WORKDIR = "/testbed"
PER_COMMAND_MS = 300_000
TOTAL_MS = 900_000


def image_for(instance_id: str) -> str:
    """The published instance image, by the dataset's naming rule."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def fixture_id_for(instance_id: str) -> str:
    """`pallets__flask-4045` becomes `pallets-flask-4045`, a safe path segment."""
    return instance_id.replace("__", "-")


def ids_in(row: Mapping[str, object], key: str) -> tuple[str, ...]:
    """A test list from a row. The dataset stores it as a JSON string."""
    value = row[key]
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list) or not all(isinstance(t, str) for t in decoded):
        raise ValueError(f"{key} is not a list of test ids")
    return tuple(decoded)


def runnable(ids: Sequence[str]) -> tuple[str, ...]:
    """The ids as pytest can select them, each once, in order.

    The dataset cuts a parametrised id at its first space, because its log
    parser splits on whitespace: `test_exprs[multi-line string]` is stored as
    `test_exprs[multi-line`. SWE-bench's harness reads logs, so it never runs
    the id and never notices; pytest, given it, reports "not found" and fails
    the whole command. A cut id is therefore widened to its test function,
    which runs every parameter of that function: a small superset of the tests
    the dataset names, never a subset.
    """
    seen: dict[str, None] = {}
    for test in ids:
        if "[" in test and not test.endswith("]"):
            test = test[: test.index("[")]
        seen.setdefault(test)
    return tuple(seen)


def pytest_command(ids: Sequence[str]) -> str:
    """One pytest invocation over every id, run in the image's environment.

    `HOME=/tmp` because the container runs as the host user, who has no home
    directory in the image. Each id is quoted: parametrised ids carry brackets
    and spaces that the shell would otherwise split or glob.
    """
    quoted = " ".join(shlex.quote(test) for test in ids)
    return f"HOME=/tmp {INTERPRETER} -m pytest -rA -p no:cacheprovider {quoted}"


def _string(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def render_task(
    row: Mapping[str, object],
    *,
    fixture_sha256: str,
    base_image: str,
    test_patch_file: str,
    test_patch_sha256: str,
) -> str:
    """The task file for one instance, as text."""
    instance_id = str(row["instance_id"])
    fixture_id = fixture_id_for(instance_id)
    fail_to_pass = runnable(ids_in(row, "FAIL_TO_PASS"))
    pass_to_pass = runnable(ids_in(row, "PASS_TO_PASS"))
    lines = [
        f"# SWE-bench Lite instance {instance_id}, written by af import-swebench.",
        "schema_version = 1",
        "",
        "[task]",
        f"id = {_string(fixture_id)}",
        'suite = "oss"',
        f"description = {_string(str(row['problem_statement']).strip())}",
        "",
        "[fixture]",
        f"id = {_string(fixture_id)}",
        f'sha256 = "{fixture_sha256}"',
        f"test_patch = {_string(test_patch_file)}",
        f'test_patch_sha256 = "{test_patch_sha256}"',
        "",
        "[environment]",
        f"base_image = {_string(base_image)}",
        f"image = {_string('afrunner-oss:' + fixture_id)}",
        f'workdir = "{WORKDIR}"',
        "",
        "[timeouts]",
        f"per_command_ms = {PER_COMMAND_MS}",
        f"total_ms = {TOTAL_MS}",
        "",
        "[[commands]]",
        'role = "fail_to_pass"',
        f"command = {_string(pytest_command(fail_to_pass))}",
    ]
    if pass_to_pass:
        lines += [
            "",
            "[[commands]]",
            'role = "pass_to_pass"',
            f"command = {_string(pytest_command(pass_to_pass))}",
        ]
    return "\n".join(lines) + "\n"
