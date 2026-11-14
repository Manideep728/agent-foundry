"""The command line's guards for real-repository tasks.

Each refusal happens before a run exists: the agent or runner asked for cannot
measure a whole repository, and a run that tried would fail for that reason
and look like the agent's result.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control_plane.interface.cli import main

REPO = Path(__file__).resolve().parent.parent
SYNTHETIC = REPO / "tasks" / "csv-quoted-fields.toml"


def oss_task(tmp_path: Path) -> Path:
    """The synthetic task, moved into the oss suite with an environment."""
    text = SYNTHETIC.read_text(encoding="utf-8").replace(
        'suite = "synthetic"', 'suite = "oss"'
    )
    environment = '[environment]\nimage = "afrunner-oss:test"\nworkdir = "/testbed"'
    text = text.replace("[timeouts]", f"{environment}\n\n[timeouts]")
    path = tmp_path / "tasks" / "oss-task.toml"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--runner", "local"], "--runner local has none"),
        (["--agent", "ollama"], "does not fit one prompt"),
        (["--agent", "single-shot"], "does not fit one prompt"),
    ],
)
def test_an_oss_task_refuses_what_cannot_measure_it(
    flags: list[str],
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = oss_task(tmp_path)
    db = tmp_path / "af.sqlite3"

    assert main(["run", str(task), "--db", str(db), *flags]) == 2
    assert message in capsys.readouterr().err
    assert not db.exists()


def test_bench_refuses_a_directory_holding_an_oss_task_for_single_shot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = oss_task(tmp_path)
    shutil.copy(SYNTHETIC, task.parent / "csv.toml")

    code = main(
        [
            "bench",
            str(task.parent),
            "--db",
            str(tmp_path / "af.sqlite3"),
            "--agent",
            "ollama",
        ]
    )

    assert code == 2
    assert "is a whole repository" in capsys.readouterr().err


def test_fetch_on_a_directory_without_tasks_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fetch", str(tmp_path)]) == 2
    assert "no task files" in capsys.readouterr().err


def test_import_names_instances_the_dataset_does_not_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "lite.jsonl"
    dataset.write_text('{"instance_id": "a__b-1"}\n', encoding="utf-8")

    code = main(["import-swebench", "x__y-9", "--dataset", str(dataset)])

    assert code == 2
    assert "x__y-9" in capsys.readouterr().err
