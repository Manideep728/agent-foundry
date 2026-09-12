"""CLI wiring."""

from __future__ import annotations

import pytest

from control_plane import __version__
from control_plane.interface.cli import main


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
