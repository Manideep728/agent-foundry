"""Choosing where the model runs: `--provider` and the flags that go with it.

Every refusal is checked before a run exists, so a typo costs no row, no
workspace and no request to a rate-limited tier.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from control_plane.adapters.single_shot_agent import SingleShotAgent
from control_plane.adapters.sqlite_repo import SqliteRunRepository
from control_plane.adapters.system import SystemClock
from control_plane.adapters.tool_use_agent import ToolUseAgent
from control_plane.interface.cli import build_parser, main
from control_plane.interface.cli.wiring import build_agent
from tests.openai_support import FakeServer, Reply, completion, fake_openai_server

REPO = Path(__file__).resolve().parent.parent
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"
KEY = "sk-test-key-that-must-not-be-stored"
KEY_ENV = "AF_TEST_PROVIDER_KEY"

HOSTED = [
    "--provider",
    "openai-compat",
    "--base-url",
    "https://api.example-tier.dev/v1",
    "--model",
    "big-model-70b",
]


@pytest.fixture
def server() -> Iterator[FakeServer]:
    with fake_openai_server() as running:
        yield running


def parse(*flags: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(["run", str(TASK_FILE), *flags])


def test_a_hosted_provider_is_named_by_its_host() -> None:
    """The provider is half of the grouping key; a generic label would merge tiers."""
    agent = build_agent(parse("--agent", "tools", *HOSTED), SystemClock())

    assert isinstance(agent, ToolUseAgent)
    assert (agent.provider_id, agent.model_id) == (
        "api.example-tier.dev",
        "big-model-70b",
    )


def test_the_provider_name_can_be_given_explicitly() -> None:
    agent = build_agent(
        parse("--agent", "single-shot", *HOSTED, "--provider-name", "example"),
        SystemClock(),
    )

    assert isinstance(agent, SingleShotAgent)
    assert agent.provider_id == "example"


def test_ollama_remains_the_name_of_the_local_single_shot_agent() -> None:
    agent = build_agent(parse("--agent", "ollama", "--model", "m:7b"), SystemClock())

    assert isinstance(agent, SingleShotAgent)
    assert agent.provider_id == "ollama"


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (
            ["--agent", "tools", "--provider", "openai-compat", "--model", "m"],
            "needs --base-url",
        ),
        (
            ["--agent", "tools", "--provider", "openai-compat", "--base-url", "x"],
            "must be an http(s) URL",
        ),
        (
            [
                "--agent",
                "tools",
                "--provider",
                "openai-compat",
                "--base-url",
                "https://h/v1",
            ],
            "needs --model",
        ),
        (["--agent", "tools", "--base-url", "https://h/v1"], "applies to --provider"),
        (["--agent", "fake", *HOSTED], "calls no model"),
        (["--agent", "tools", *HOSTED, "--api-key-env", "AF_UNSET_FOR_TEST"], "unset"),
    ],
)
def test_provider_flags_that_cannot_be_honoured_are_usage_errors(
    flags: list[str],
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "af.sqlite3"

    code = main(["run", str(TASK_FILE), "--db", str(db), *flags])

    assert code == 2
    assert message in capsys.readouterr().err
    assert not db.exists(), "a refused flag must not create a run"


def test_a_hosted_run_is_measured_and_its_key_is_stored_nowhere(
    server: FakeServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end through `af run`, with the key in the environment only."""
    monkeypatch.setenv(KEY_ENV, KEY)
    server.replies.append(Reply(body=completion("I will not use any tools.")))
    db = tmp_path / "af.sqlite3"
    runs = tmp_path / "runs"

    code = main(
        [
            "run",
            str(TASK_FILE),
            "--db",
            str(db),
            "--runs",
            str(runs),
            "--runner",
            "local",
            "--agent",
            "tools",
            "--provider",
            "openai-compat",
            "--base-url",
            server.url,
            "--model",
            "big-model-70b",
            "--api-key-env",
            KEY_ENV,
        ]
    )

    assert code == 0, capsys.readouterr().err
    assert server.received[0]["authorization"] == f"Bearer {KEY}"
    with SqliteRunRepository(db) as repository:
        (record,) = repository.list_runs()
    assert (record.provider_id, record.model_id) == ("127.0.0.1", "big-model-70b")
    assert record.invocations[0].usage.input_tokens == 12

    assert KEY.encode() not in db.read_bytes()
    for path in runs.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
