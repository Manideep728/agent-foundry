"""CLI wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane import __version__
from control_plane.interface.cli import main

UNREACHABLE = "127.0.0.1:1"


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # argparse's `version` action raises SystemExit(0) after printing.
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bare_invocation_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exit 2 is the conventional usage-error code, and matters here because the
    # runner's own exit-code table reserves specific values; the CLI must not
    # return 0 when it did nothing.
    assert main([]) == 2
    assert "no command given" in capsys.readouterr().err


def test_installed_version_matches_package_version() -> None:
    # hatchling reads the version out of control_plane/__init__.py, so a drift
    # here means the build backend configuration broke.
    from importlib.metadata import version

    assert version("agentfoundry") == __version__


def test_fixture_hash_prints_the_same_digest_the_verifier_computes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The reason the command exists. A digest produced any other way -- a
    # shasum of a tarball, say -- would surface much later as an unexplained
    # FIXTURE_HASH_MISMATCH with nothing pointing at the cause.
    from pathlib import Path

    from control_plane.adapters.fixtures import digest_tree

    fixture = (
        Path(__file__).resolve().parent.parent
        / "tasks"
        / "fixtures"
        / "csv-quoted-fields"
    )
    expected, _ = digest_tree(fixture)

    assert main(["fixture-hash", str(fixture)]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_fixture_hash_can_print_the_manifest_for_diffing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parent.parent
        / "tasks"
        / "fixtures"
        / "csv-quoted-fields"
    )
    assert main(["fixture-hash", "--manifest", str(fixture)]) == 0

    out = capsys.readouterr().out
    assert out.startswith("agentfoundry-fixture-v1\n")
    assert "csvkit/fields.py" in out


def test_fixture_hash_on_a_missing_directory_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["fixture-hash", "/definitely/not/here"]) == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "bench", "dispatch", "worker"])
def test_an_ollama_timeout_below_one_second_is_a_usage_error(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every path points somewhere empty or unreachable. If the check ever
    stops refusing, the command fails on a missing task instead of measuring
    the real suite into the default database and joining a real broker."""
    target = tmp_path / "task.toml" if command == "run" else tmp_path
    isolated = {
        "run": ["--db", str(tmp_path / "af.sqlite3"), "--runs", str(tmp_path)],
        "bench": ["--db", str(tmp_path / "af.sqlite3"), "--runs", str(tmp_path)],
        "dispatch": ["--bootstrap", UNREACHABLE],
        "worker": [
            "--db",
            str(tmp_path / "af.sqlite3"),
            "--runs",
            str(tmp_path),
            "--bootstrap",
            UNREACHABLE,
        ],
    }[command]

    code = main([command, str(target), *isolated, "--ollama-timeout", "0"])

    assert code == 2
    assert "--ollama-timeout must be at least 1" in capsys.readouterr().err
