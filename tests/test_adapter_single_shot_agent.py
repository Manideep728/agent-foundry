"""The single-shot agent, against canned replies.

No model. Every rule here is about what the harness does with a reply, and a
real model would make those rules untestable by making the reply unpredictable.
The one thing a live model proves -- that a real server answers in the expected
shape -- is tested in `test_adapter_ollama_client.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.adapters.diffing import diff_workspace
from control_plane.adapters.single_shot_agent import SingleShotAgent
from control_plane.adapters.task_toml import load_task
from control_plane.adapters.workspace import WorkspaceFactory
from control_plane.domain.fixture import FixtureEntry
from control_plane.domain.ids import RunId
from control_plane.domain.model import Message, ModelResponse, ModelUsage
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.task import TaskDefinition
from control_plane.services.errors import AgentError
from tests.fakes import FakeClock

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tasks" / "fixtures"
TASK_FILE = REPO / "tasks" / "csv-quoted-fields.toml"

FIELDS = "csvkit/fields.py"


class StubClient:
    """Returns one canned reply and remembers what it was asked."""

    def __init__(self, text: str, *, truncated: bool = False) -> None:
        self._text = text
        self._truncated = truncated
        self.prompts: list[tuple[Message, ...]] = []

    @property
    def provider_id(self) -> str:
        return "stub-provider"

    @property
    def model_id(self) -> str:
        return "stub-1"

    def complete(self, messages: tuple[Message, ...]) -> ModelResponse:
        self.prompts.append(messages)
        return ModelResponse(
            text=self._text,
            usage=ModelUsage(input_tokens=120, output_tokens=40),
            truncated=self._truncated,
        )


def prepared(tmp_path: Path) -> tuple[TaskDefinition, Path, tuple[FixtureEntry, ...]]:
    task = load_task(TASK_FILE)
    factory = WorkspaceFactory(tmp_path / "runs")
    workspace = factory.create(RunId("run-0001"))
    _, entries = factory.copy_fixture(workspace, FIXTURES / task.fixture_id)
    return task, workspace.repo, entries


def block(path: str, body: str) -> str:
    return f"```path={path}\n{body}\n```"


def run(tmp_path: Path, reply: str, *, truncated: bool = False, **kwargs: object):  # type: ignore[no-untyped-def]
    task, repo, entries = prepared(tmp_path)
    client = StubClient(reply, truncated=truncated)
    agent = SingleShotAgent(client, FakeClock(), **kwargs)  # type: ignore[arg-type]
    return agent.edit(repo, task), repo, entries, client, task


# --- the happy path ----------------------------------------------------------


def test_a_tagged_block_rewrites_that_file(tmp_path: Path) -> None:
    result, repo, entries, _, task = run(
        tmp_path, block(FIELDS, "def split_fields(row):\n    return ['ok']")
    )

    assert result.outcome is None
    assert "return ['ok']" in (repo / FIELDS).read_text()
    patch = diff_workspace(entries, FIXTURES / task.fixture_id, repo)
    assert patch.paths == (FIELDS,)


def test_the_invocation_reports_the_model_not_the_wrapper(tmp_path: Path) -> None:
    """A report that said "single-shot" would hide which model produced the
    number, and D6 requires every run to name its adapter."""
    result, _, _, _, _ = run(tmp_path, block(FIELDS, "x = 1"))

    assert len(result.invocations) == 1
    assert result.invocations[0].provider_id == "stub-provider"
    assert result.invocations[0].model_id == "stub-1"
    assert result.invocations[0].cost == 0  # a local model is free, not unknown


# --- replies that cannot be read ---------------------------------------------


def test_prose_with_no_block_is_malformed_not_an_empty_patch(tmp_path: Path) -> None:
    """The distinction the whole outcome override exists for.

    Both this and a deliberate no-op leave the workspace untouched. Only the
    agent knows which happened, and conflating them makes the malformed-output
    rate unmeasurable.
    """
    result, repo, entries, _, task = run(
        tmp_path, "I would change the split_fields function to handle quotes."
    )

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    patch = diff_workspace(entries, FIXTURES / task.fixture_id, repo)
    assert patch.is_empty


def test_a_truncated_reply_is_malformed_even_when_it_parses(tmp_path: Path) -> None:
    """A reply cut off at the output cap often parses into a half-written file.

    Applying it would score as a wrong answer when the real cause is a limit
    the harness chose, so the cap is checked before the text is read.
    """
    result, repo, _, _, _ = run(
        tmp_path, block(FIELDS, "def split_fields(row):"), truncated=True
    )

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert "def split_fields(row):\n" not in (repo / FIELDS).read_text()


def test_an_unclosed_block_is_malformed(tmp_path: Path) -> None:
    result, repo, _, _, _ = run(tmp_path, f"```path={FIELDS}\nx = 1\n")

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert "x = 1" not in (repo / FIELDS).read_text()


def test_the_same_path_twice_is_malformed(tmp_path: Path) -> None:
    """Two versions of one file, with no way to know which was meant."""
    result, _, _, _, _ = run(
        tmp_path, block(FIELDS, "x = 1") + "\n" + block(FIELDS, "x = 2")
    )

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT


# --- the security boundary ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escaped.py",
        "../../etc/passwd",
        "/etc/passwd",
        "csvkit/../../escaped.py",
        "csvkit\\fields.py",
    ],
)
def test_a_path_outside_the_workspace_is_refused(tmp_path: Path, path: str) -> None:
    """The one place model output becomes a filesystem write.

    A benchmark runs whatever a model emits, so this is the boundary that stops
    a reply from editing the harness that scores it.
    """
    result, repo, _, _, _ = run(tmp_path, block(path, "pwned = True"))

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert not (repo.parent / "escaped.py").exists()
    assert not (repo.parent.parent / "escaped.py").exists()


def test_a_symlink_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    """What the resolved-parent check catches and the path rule cannot.

    `escape/evil.py` is a perfectly legal fixture path -- no traversal segment,
    no leading slash -- so `validate_fixture_path` accepts it. Only resolving
    the target and comparing it with the workspace root sees that the first
    segment is a symlink pointing somewhere else.

    Nothing in the harness puts a symlink in a workspace today: `copy_fixture`
    passes `symlinks=False` and the agent cannot create one. This is the layer
    that holds if that ever stops being true.
    """
    task, repo, _ = prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    client = StubClient(block("escape/evil.py", "pwned = True"))

    result = SingleShotAgent(client, FakeClock()).edit(repo, task)

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert not (outside / "evil.py").exists()


def test_nothing_is_written_when_a_later_block_is_unusable(tmp_path: Path) -> None:
    """A half-applied reply would be scored as the model's patch."""
    reply = block(FIELDS, "first = True") + "\n" + block("../escaped.py", "second")

    result, repo, entries, _, task = run(tmp_path, reply)

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert "first = True" not in (repo / FIELDS).read_text()
    patch = diff_workspace(entries, FIXTURES / task.fixture_id, repo)
    assert patch.is_empty


# --- what the model is shown -------------------------------------------------


def test_hidden_paths_are_kept_out_of_the_prompt(tmp_path: Path) -> None:
    """D18. The tests that decide the score live in the fixture, so an agent
    handed the whole repository is handed the answer."""
    task, repo, _ = prepared(tmp_path)
    hidden = "tests/test_quoting.py"
    task = TaskDefinition(
        task_id=task.task_id,
        suite=task.suite,
        description=task.description,
        fixture_id=task.fixture_id,
        fixture_sha256=task.fixture_sha256,
        commands=task.commands,
        per_command_timeout_ms=task.per_command_timeout_ms,
        total_timeout_ms=task.total_timeout_ms,
        hidden_paths=(hidden,),
    )
    client = StubClient(block(FIELDS, "x = 1"))

    SingleShotAgent(client, FakeClock()).edit(repo, task)

    prompt = client.prompts[0][1].content
    assert f"path={hidden}" not in prompt
    # The file is still in the workspace: hiding governs the prompt only, and
    # validation still runs it.
    assert (repo / hidden).is_file()
    # A visible test file is still shown, as it would be at a base commit.
    assert "path=tests/test_basic.py" in prompt


def test_excluded_directories_never_reach_the_prompt(tmp_path: Path) -> None:
    """`is_excluded` is shared with the hasher and the copier. A third spelling
    of that rule is how `__pycache__` ends up in a prompt."""
    task, repo, _ = prepared(tmp_path)
    junk = repo / "__pycache__"
    junk.mkdir(exist_ok=True)
    (junk / "fields.cpython-312.pyc").write_bytes(b"\x00\x01binary")
    client = StubClient(block(FIELDS, "x = 1"))

    SingleShotAgent(client, FakeClock()).edit(repo, task)

    assert "__pycache__" not in client.prompts[0][1].content


def test_the_prompt_carries_the_task_description(tmp_path: Path) -> None:
    task, repo, _ = prepared(tmp_path)
    client = StubClient(block(FIELDS, "x = 1"))

    SingleShotAgent(client, FakeClock()).edit(repo, task)

    assert task.description.strip() in client.prompts[0][1].content


def test_a_fixture_too_large_to_send_is_a_harness_failure(tmp_path: Path) -> None:
    """Not a measurement. The model never saw anything, so it cannot have
    failed; recording this as an agent result would blame it for a limit the
    harness set.
    """
    task, repo, _ = prepared(tmp_path)
    agent = SingleShotAgent(StubClient("unused"), FakeClock(), max_prompt_bytes=10)

    with pytest.raises(AgentError, match="more than this agent sends"):
        agent.edit(repo, task)


# --- keeping the reply that could not be read -------------------------------


def test_a_malformed_reply_is_kept_with_the_reason_it_was_malformed(
    tmp_path: Path,
) -> None:
    """A malformed_output rate nobody can inspect cannot be acted on.

    The text used to be a local variable and the reason an exception caught
    bare, so both were discarded by the handler that classified the run. What
    survived was the verdict with none of the evidence.
    """
    reply = "I think the fix is to change the parser, but I will not write it."
    result, _, _, _, _ = run(tmp_path, reply)

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.raw_reply == reply
    assert result.outcome_detail is not None
    assert "no tagged file block" in result.outcome_detail


def test_a_reply_cut_off_by_the_output_cap_says_so(tmp_path: Path) -> None:
    # Different causes, different fixes: an output cap is a harness choice
    # and a missing fence is the model. Both score malformed_output, so the
    # note is the only thing that tells them apart.
    result, _, _, _, _ = run(
        tmp_path, block("csvkit/fields.py", "x = 1"), truncated=True
    )

    assert result.outcome is AgentOutcome.MALFORMED_OUTPUT
    assert result.outcome_detail == "the reply hit the output cap"


def test_a_reply_that_parsed_keeps_nothing(tmp_path: Path) -> None:
    # patch.diff already records what a working run did, in a more useful form.
    result, _, _, _, _ = run(tmp_path, block("csvkit/fields.py", "x = 1"))

    assert result.outcome is None
    assert result.raw_reply is None
    assert result.outcome_detail is None
