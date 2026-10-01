"""Run artifacts in S3, keyed on the run id (D34).

A run's directory holds what a reader needs to check a measurement after the
machine that took it is gone: the patch, the command file, `result.json`, the
captured logs, the fixture manifest, and the agent's reply when it was kept.
They go to `s3://<bucket>/<prefix>/runs/<run_id>/`, file by file, under the
same relative paths.

`repo/` is left out. It is the copy of the fixture the agent edited, already
summarised by `patch.diff` and reproducible from the fixture and the patch;
and the service deletes it after a scored run anyway.

The location is derived from the run id rather than stored, so a record and
its files are joined by the id alone and the database schema does not change.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from control_plane.domain.record import RunRecord
from control_plane.services.ports import ArtifactsNotStored

if TYPE_CHECKING:  # pragma: no cover - import only for the type checker
    from mypy_boto3_s3 import S3Client

_SCHEME = "s3://"
_WORKSPACE_COPY = "repo"


def parse_location(uri: str) -> tuple[str, str]:
    """`s3://bucket/some/prefix` as `("bucket", "some/prefix")`."""
    if not uri.startswith(_SCHEME):
        raise ValueError(f"an artifact location is s3://bucket[/prefix], got {uri}")
    bucket, _, prefix = uri[len(_SCHEME) :].partition("/")
    if not bucket:
        raise ValueError(f"no bucket in {uri}")
    return bucket, prefix.strip("/")


class S3ArtifactStore:
    """Uploads each finished run's files to one bucket and prefix."""

    def __init__(self, location: str, client: S3Client | None = None) -> None:
        self._bucket, self._prefix = parse_location(location)
        if client is None:
            try:
                import boto3
            except ImportError as error:
                raise RuntimeError(
                    "an s3:// artifact location needs boto3: uv sync --extra aws"
                ) from error
            client = boto3.client("s3")
        self._client = client

    def location_of(self, run_id: str) -> str:
        return f"{_SCHEME}{self._bucket}/{self._key_root(run_id)}"

    def put_run(self, record: RunRecord) -> str:
        run_id = str(record.run.run_id)
        if record.workspace_path is None:
            raise ArtifactsNotStored(f"{run_id} names no run directory")
        directory = Path(record.workspace_path)
        if not directory.is_dir():
            # Not an empty upload. A run whose files are gone cannot be checked,
            # and a location that looks complete would say it could.
            raise ArtifactsNotStored(f"{run_id}: {directory} does not exist")

        root = self._key_root(run_id)
        # Imported here, so the module loads without the extra installed.
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(directory)
                if not path.is_file() or relative.parts[0] == _WORKSPACE_COPY:
                    continue
                # One request per file rather than `upload_file`: the files are
                # small (logs are capped by the runner), and the transfer
                # manager's threads and its own exception type buy nothing.
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=f"{root}{relative.as_posix()}",
                    Body=path.read_bytes(),
                )
        except (BotoCoreError, ClientError, OSError) as error:
            raise ArtifactsNotStored(f"{run_id}: {error}") from error
        return self.location_of(run_id)

    def _key_root(self, run_id: str) -> str:
        prefix = f"{self._prefix}/" if self._prefix else ""
        return f"{prefix}runs/{run_id}/"
