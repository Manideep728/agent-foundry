"""Whether the tests that start real containers can run on this machine."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

IMAGE = os.environ.get("RUNNER_IMAGE", "afrunner:local")


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
    )
    return probe.returncode == 0


requires_image = pytest.mark.skipif(
    not _image_available(),
    reason=f"docker or the {IMAGE} image is unavailable; run `make runner-image`",
)
