"""Docker steps that make a real-repository task runnable offline (D39).

Three steps, each one `docker` call or a short sequence of them: pull the
instance image, copy its source directory out as the fixture, and build the
validation image from it with the runner added. All of them need the network
or a large download, which is why they are a separate command (`af fetch`) and
never part of a run.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

_PULL_TIMEOUT_S = 1_800
_CALL_TIMEOUT_S = 120
_BUILD_TIMEOUT_S = 900


class FetchFailed(Exception):
    """A step of `af fetch` failed; the message says which and why."""


def _docker(argv: list[str], timeout_s: int, what: str) -> bytes:
    try:
        completed = subprocess.run(
            ["docker", *argv], capture_output=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as error:
        raise FetchFailed("docker is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise FetchFailed(f"{what} did not finish within {timeout_s} s") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise FetchFailed(f"{what} failed: {detail}")
    return completed.stdout


def ensure_pulled(reference: str) -> None:
    """Pull the image unless it is already here. A pinned digest never changes."""
    try:
        _docker(["image", "inspect", reference], _CALL_TIMEOUT_S, "inspect")
        return
    except FetchFailed:
        pass
    _docker(["pull", reference], _PULL_TIMEOUT_S, f"docker pull {reference}")


def pinned_reference(reference: str) -> str:
    """`name@sha256:...` for a pulled image, so a task names exact bytes."""
    out = _docker(
        ["image", "inspect", "--format", "{{index .RepoDigests 0}}", reference],
        _CALL_TIMEOUT_S,
        f"reading the digest of {reference}",
    )
    pinned = out.decode("ascii", errors="replace").strip()
    if "@sha256:" not in pinned:
        raise FetchFailed(f"{reference} has no registry digest to pin: {pinned!r}")
    return pinned


def extract_fixture(image: str, workdir: str, destination: Path) -> None:
    """Copy `workdir` out of `image` into `destination`, without `.git`.

    Through a created container, which runs nothing, and a tar stream that
    `tarfile`'s `data` filter unpacks: it refuses absolute paths, links that
    leave the destination and device files, so an image cannot write outside
    the cache by way of its own archive.
    """
    container = (
        _docker(["create", image], _CALL_TIMEOUT_S, f"docker create {image}")
        .decode("ascii")
        .strip()
    )
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
            _copy_out(container, workdir, Path(scratch))
            unpacked = Path(scratch) / PurePosixPath(workdir).name
            if not unpacked.is_dir():
                raise FetchFailed(f"{image} has no directory at {workdir}")
            shutil.rmtree(unpacked / ".git", ignore_errors=True)
            if destination.exists():
                shutil.rmtree(destination)
            unpacked.rename(destination)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container], capture_output=True, check=False
        )


def _copy_out(container: str, workdir: str, into: Path) -> None:
    with subprocess.Popen(
        ["docker", "cp", f"{container}:{workdir}", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdout is not None
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                archive.extractall(into, filter="data")
        except tarfile.TarError as error:
            raise FetchFailed(
                f"the archive of {workdir} is unreadable: {error}"
            ) from error
        stderr = process.stderr.read() if process.stderr else b""
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise FetchFailed(f"docker cp {workdir} failed: {detail}")


def build_validation_image(base_image: str, tag: str, dockerfile_dir: Path) -> None:
    """The task's environment with afrunner added (`infra/oss/Dockerfile`)."""
    _docker(
        [
            "build",
            "--build-arg",
            f"BASE_IMAGE={base_image}",
            "--tag",
            tag,
            str(dockerfile_dir),
        ],
        _BUILD_TIMEOUT_S,
        f"building {tag}",
    )
