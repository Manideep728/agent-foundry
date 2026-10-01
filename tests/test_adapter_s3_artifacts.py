"""The S3 artifact store, against moto's in-process S3.

moto answers the real S3 API through the real boto3 client, so these test the
keys, the exclusions and the failure path, not a mock of our own making.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from control_plane.adapters.s3_artifacts import S3ArtifactStore, parse_location
from control_plane.domain.record import RunRecord
from control_plane.services.ports import ArtifactsNotStored
from tests.test_repository_contract import base_record, new_run

BUCKET = "af-artifacts"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    # Credentials and a region moto accepts; nothing here can reach AWS.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


def a_run_directory(tmp_path: Path) -> RunRecord:
    """The layout the workspace factory leaves after a run."""
    root = tmp_path / "runs" / "run-0001"
    for relative, body in {
        "patch.diff": "--- a/x\n+++ b/x\n",
        "manifest.txt": "x 1\n",
        "in/commands.txt": "python3 -m unittest\n",
        "out/result.json": "{}\n",
        "out/logs/cmd-000.stdout.log": "ok\n",
        "repo/x.py": "print('the fixture copy')\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return base_record(new_run(), workspace_path=str(root))


def keys(client: object) -> list[str]:
    listed = client.list_objects_v2(Bucket=BUCKET)  # type: ignore[attr-defined]
    return sorted(item["Key"] for item in listed.get("Contents", []))


def test_a_run_is_stored_under_its_id_without_the_fixture_copy(
    s3: object, tmp_path: Path
) -> None:
    store = S3ArtifactStore(f"s3://{BUCKET}/bench-1")

    location = store.put_run(a_run_directory(tmp_path))

    assert location == f"s3://{BUCKET}/bench-1/runs/run-0001/"
    assert keys(s3) == [
        "bench-1/runs/run-0001/in/commands.txt",
        "bench-1/runs/run-0001/manifest.txt",
        "bench-1/runs/run-0001/out/logs/cmd-000.stdout.log",
        "bench-1/runs/run-0001/out/result.json",
        "bench-1/runs/run-0001/patch.diff",
    ]


def test_storing_a_run_twice_leaves_the_same_objects(
    s3: object, tmp_path: Path
) -> None:
    """The retry path uploads again; it must neither duplicate nor change."""
    store = S3ArtifactStore(f"s3://{BUCKET}")
    record = a_run_directory(tmp_path)

    store.put_run(record)
    first = keys(s3)
    store.put_run(record)

    assert keys(s3) == first
    body = s3.get_object(  # type: ignore[attr-defined]
        Bucket=BUCKET, Key="runs/run-0001/patch.diff"
    )["Body"].read()
    assert body == b"--- a/x\n+++ b/x\n"


def test_an_unreachable_bucket_is_a_failure_to_store(
    s3: object, tmp_path: Path
) -> None:
    store = S3ArtifactStore("s3://no-such-bucket")

    with pytest.raises(ArtifactsNotStored, match="run-0001"):
        store.put_run(a_run_directory(tmp_path))


def test_a_run_whose_directory_is_gone_is_not_stored_as_empty(
    s3: object, tmp_path: Path
) -> None:
    record = dataclasses.replace(
        a_run_directory(tmp_path), workspace_path=str(tmp_path / "gone")
    )

    with pytest.raises(ArtifactsNotStored, match="does not exist"):
        S3ArtifactStore(f"s3://{BUCKET}").put_run(record)
    assert keys(s3) == []


@pytest.mark.parametrize(
    ("uri", "parsed"),
    [
        ("s3://bucket", ("bucket", "")),
        ("s3://bucket/", ("bucket", "")),
        ("s3://bucket/a/b/", ("bucket", "a/b")),
    ],
)
def test_a_location_names_a_bucket_and_an_optional_prefix(
    uri: str, parsed: tuple[str, str]
) -> None:
    assert parse_location(uri) == parsed


@pytest.mark.parametrize("uri", ["bucket/prefix", "s3://", "https://bucket"])
def test_a_location_that_is_not_s3_is_refused(uri: str) -> None:
    with pytest.raises(ValueError):
        parse_location(uri)
