"""An agent that asks for the whole file back, once.

One call, no tools, no turns. That is a deliberate floor rather than an
ambition: a 3B model will not emit an applyable unified diff, and a malformed
diff is indistinguishable from a wrong one, so the reply asks for complete file
contents instead. Whether the model succeeds is a measurement; whether the
harness can tell success from garbage is this module's job.

The agent writes files and reports what it consumed. It never reports what it
changed -- `diff_workspace` reads the workspace for that -- so an agent cannot
misstate its own patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from control_plane.domain.errors import DomainError
from control_plane.domain.fixture import is_excluded, validate_fixture_path
from control_plane.domain.model import (
    Message,
    MessageRole,
    ModelClient,
    ModelInvocation,
    TokenPrice,
    cost_micros,
)
from control_plane.domain.outcomes import AgentOutcome
from control_plane.domain.ports import Clock
from control_plane.domain.task import TaskDefinition
from control_plane.services.errors import AgentError
from control_plane.services.ports import AgentResult

DEFAULT_MAX_PROMPT_BYTES = 200_000
"""A ceiling on the repository text put into one prompt.

Not a model context limit -- it is a harness limit, deliberately separate. A
fixture that outgrows a single-shot prompt needs a different agent, and saying
so is better than silently sending a truncated repository and then recording
the model's confusion as a result.
"""

_BLOCK_START = re.compile(r"^`{3,}\s*path\s*=\s*(?P<path>[^\s`]+)\s*$")
_BLOCK_END = re.compile(r"^`{3,}\s*$")

_SYSTEM_PROMPT = """You repair a small software repository.

You will be given a problem statement and the current contents of every file
you are allowed to see. Return the complete new contents of each file you
change, and nothing else.

Use exactly this format for every file you change:

```path=relative/path/to/file.py
<the complete new contents of that file>
```

Rules:
- Give the whole file, not a diff and not a fragment.
- Use the file's path exactly as it was given to you.
- Change as few files as possible.
- Write no explanation outside the blocks."""


@dataclass(frozen=True, slots=True)
class ParsedEdit:
    path: str
    content: str


class MalformedReply(Exception):
    """The reply could not be read as a set of edits.

    Internal to this module. It becomes `AgentOutcome.MALFORMED_OUTPUT`, a
    measurement, and must never escape as `AgentError`, which would make it a
    harness failure and take the run out of the benchmark entirely.
    """


class SingleShotAgent:
    """Sends the repository once and applies whatever comes back."""

    def __init__(
        self,
        client: ModelClient,
        clock: Clock,
        *,
        price: TokenPrice | None = None,
        max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
    ) -> None:
        self._client = client
        self._clock = clock
        self._price = price or TokenPrice.free(client.model_id)
        self._max_prompt_bytes = max_prompt_bytes

    @property
    def provider_id(self) -> str:
        # The provider is the model's, not this wrapper's. A report that said
        # "single-shot" would hide which model produced the number, and D6
        # requires every run to name the adapter behind it.
        return self._client.provider_id

    def edit(self, workspace: Path, task: TaskDefinition) -> AgentResult:
        prompt = self._build_prompt(workspace, task)

        started = self._clock.monotonic_ms()
        response = self._client.complete(
            (
                Message(MessageRole.SYSTEM, _SYSTEM_PROMPT),
                Message(MessageRole.USER, prompt),
            )
        )
        invocation = ModelInvocation(
            provider_id=self._client.provider_id,
            model_id=self._client.model_id,
            usage=response.usage,
            cost=cost_micros(response.usage, self._price),
            latency_ms=self._clock.monotonic_ms() - started,
        )

        try:
            if response.truncated:
                # Checked before parsing, because a truncated reply often
                # parses into a half-written file. Applying that would look
                # like a wrong answer when the real cause is an output cap the
                # harness chose.
                raise MalformedReply("the reply hit the output cap")
            edits = _parse(response.text)
            _apply(workspace, edits)
        except MalformedReply as error:
            # The text and the reason both travel back. They used to die here:
            # the reply was a local, the exception was caught bare, and the run
            # recorded that the model had answered badly while keeping nothing
            # that showed how.
            return AgentResult(
                (invocation,),
                outcome=AgentOutcome.MALFORMED_OUTPUT,
                raw_reply=response.text,
                outcome_detail=str(error),
            )

        return AgentResult((invocation,))

    def _build_prompt(self, workspace: Path, task: TaskDefinition) -> str:
        hidden = set(task.hidden_paths)
        parts = [task.description.strip(), "", "Files:"]
        total = 0

        for path in _visible_files(workspace):
            if path in hidden:
                continue
            try:
                content = (workspace / path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # A file this agent cannot read is a file it cannot rewrite.
                # Naming it is better than pretending the repository is
                # smaller than it is.
                parts.extend([f"```path={path}", "<not readable as UTF-8 text>", "```"])
                continue
            total += len(content.encode("utf-8"))
            if total > self._max_prompt_bytes:
                raise AgentError(
                    f"{task.task_id}: the visible fixture exceeds "
                    f"{self._max_prompt_bytes} bytes, which is more than this "
                    f"agent sends in one prompt"
                )
            parts.extend([f"```path={path}", content.rstrip("\n"), "```"])

        return "\n".join(parts)


def _visible_files(workspace: Path) -> list[str]:
    """Every file the agent may see, in a stable order.

    `is_excluded` is shared with the hasher and the copier rather than
    reimplemented. A third spelling of that rule is how `__pycache__` reaches a
    prompt, and how a copy and a manifest start disagreeing.
    """
    found: list[str] = []
    for entry in sorted(workspace.rglob("*")):
        if not entry.is_file():
            continue
        relative = entry.relative_to(workspace)
        if any(is_excluded(part) for part in relative.parts):
            continue
        found.append(relative.as_posix())
    return found


def _parse(text: str) -> tuple[ParsedEdit, ...]:
    """Pull tagged blocks out of a reply.

    A fence inside a file's contents ends its block early. That is accepted:
    the alternative is a length-prefixed format no small model follows, and
    this failure is visible -- the file ends up truncated and its tests fail --
    rather than silent.
    """
    edits: list[ParsedEdit] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        start = _BLOCK_START.match(lines[index])
        if start is None:
            index += 1
            continue
        path = start.group("path")
        index += 1
        body: list[str] = []
        while index < len(lines) and not _BLOCK_END.match(lines[index]):
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            raise MalformedReply(f"the block for {path} was never closed")
        index += 1
        edits.append(ParsedEdit(path=path, content="\n".join(body) + "\n"))

    if not edits:
        raise MalformedReply("the reply contained no tagged file block")

    seen = [edit.path for edit in edits]
    if len(set(seen)) != len(seen):
        # Two versions of one file, with no way to know which the model meant.
        raise MalformedReply("the reply gave the same path twice")
    return tuple(edits)


def _apply(workspace: Path, edits: tuple[ParsedEdit, ...]) -> None:
    """Write the edits, refusing anything that leaves the workspace.

    This is the one place in the project where model output becomes a
    filesystem write. `validate_fixture_path` rejects absolute paths, traversal
    segments and backslashes; the resolved-parent check catches what a symlink
    inside the workspace could still do.

    Nothing is written until every edit has passed, so a reply whose second
    block is unusable does not leave the first one applied. A half-applied
    reply would be scored as the model's patch.
    """
    root = workspace.resolve()
    planned: list[tuple[Path, str]] = []

    for edit in edits:
        try:
            validate_fixture_path(edit.path)
        except DomainError as error:
            raise MalformedReply(f"unusable path {edit.path!r}: {error}") from error

        target = (root / edit.path).resolve()
        if not target.is_relative_to(root):
            raise MalformedReply(f"path {edit.path!r} leaves the workspace")
        planned.append((target, edit.content))

    for target, content in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
