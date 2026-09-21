"""The prompts the harness chooses, each one named.

A prompt is an experimental variable, not a constant. Measured on 2026-09-20,
`llama3.2:3b` drives the tool loop, reads the repository and never writes to
it; under a blunter prompt the same model, the same temperature and the same
seed produce patches. The prompt moved the result further than the model choice
did, so a benchmark row that does not say which prompt produced it does not
say what was measured.

Hence a name on every prompt, recorded on the run and part of the grouping key.
The alternative -- editing the text in place -- makes the old runs and the new
runs one row in `af report`, which is the blended-summary defect D20 exists to
prevent, one field over.

A variant is everything the harness chose for the model to read: the system
text **and** the tool descriptions. Keeping the tools out of it would let
someone tune a description in place while the recorded id stayed the same, and
the database would then be wrong rather than incomplete.

A variant is frozen once it is named. `tests/test_adapter_prompts.py` pins a
digest of each one, so editing a shipped variant fails a test and forces a new
id -- the rule a fixture already lives under, applied to the other input that
decides a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from control_plane.domain.errors import DomainError
from control_plane.domain.model import ToolSpec

LIST_FILES = "list_files"
READ_FILE = "read_file"
WRITE_FILE = "write_file"

TOOLS = (
    ToolSpec(
        name=LIST_FILES,
        description=(
            "List every file you may read or change, as paths relative to the "
            "repository root. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name=READ_FILE,
        description="Read one file. Give its path exactly as list_files gave it.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                }
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name=WRITE_FILE,
        description=(
            "Replace one file with new contents. Give the whole file, not a "
            "diff and not a fragment."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                },
                "content": {
                    "type": "string",
                    "description": "The complete new contents of the file.",
                },
            },
            "required": ["path", "content"],
        },
    ),
)


@dataclass(frozen=True, slots=True)
class PromptVariant:
    """One named, frozen set of instructions to a model."""

    prompt_id: str
    system: str
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt_id:
            raise DomainError("a prompt variant must be named")
        if not self.system.strip():
            raise DomainError(f"{self.prompt_id} has no system text")


TOOLS_V1 = PromptVariant(
    prompt_id="tools-v1",
    system="""You repair a small software repository.

Work with the tools you are given. A typical repair is: list the files, read
the ones that matter, then write the file you need to change.

Rules:
- Use list_files before assuming a path exists.
- Give whole files to write_file, never a diff or a fragment.
- Change as few files as possible.
- Some files are not shown to you. That is deliberate; work from the ones you
  can see.
- When the repair is done, say so in a short sentence and stop calling tools.""",
    tools=TOOLS,
)
"""The prompt every tool-loop measurement before 2026-09-20 was taken under.

Kept verbatim and never edited, because the rows it produced are still in the
database and still quoted. Under it, `llama3.2:3b` calls one to twelve tools
and writes nothing, and `qwen2.5-coder:7b` scores 0/8.
"""

TOOLS_V2 = PromptVariant(
    prompt_id="tools-v2",
    system="""You repair a repository. You must end by calling write_file.

Every task you are given contains a defect that makes a test fail. Your job is
not to explain it. Your job is to call write_file with a corrected file.

Do exactly this:
1. Call list_files.
2. Call read_file on the file that looks wrong.
3. Call write_file with that same path and the entire corrected file.

Never answer in prose before write_file has succeeded. If you are unsure what
is wrong, read another file and then write your best correction anyway. A
wrong patch is a result; no patch is not.

write_file takes the whole file. Never a diff, never a fragment, never an
excerpt with "..." standing in for lines you did not repeat.""",
    tools=TOOLS,
)
"""The first prompt that made a local model write.

Three properties carried the change, as far as a three-task probe can say: the
first line states the tool the model must reach, a numbered order ends at
`write_file`, and the model is told that a wrong patch beats no patch. That
last line is there because the observed failure was a model declining to act
while it was unsure, and a benchmark cannot score restraint.

Two temptations deliberately refused. There is no "write the file back
unchanged if nothing is wrong": it would manufacture a no-op write, and
`agent_note`'s only distinction -- investigated and declined, against wrote
something -- would be gone. And the tool descriptions are byte-identical to
`tools-v1`, so a difference in the measurement is a difference in the system
text and nothing else.
"""

SINGLE_SHOT_V1 = PromptVariant(
    prompt_id="single-shot-v1",
    system="""You repair a small software repository.

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
- Write no explanation outside the blocks.""",
)
"""The single-shot agent sends no tools, so its variant declares none."""

VARIANTS = MappingProxyType(
    {variant.prompt_id: variant for variant in (TOOLS_V1, TOOLS_V2, SINGLE_SHOT_V1)}
)

DEFAULT_TOOL_PROMPT = TOOLS_V1.prompt_id
"""What `--agent tools` uses when no prompt is named.

Still `tools-v1`, until a measurement says otherwise. Making the untested
prompt the default would silently change what every later run means.
"""

DEFAULT_SINGLE_SHOT_PROMPT = SINGLE_SHOT_V1.prompt_id


def variant(prompt_id: str) -> PromptVariant:
    """The named variant, or an error naming what exists.

    Refused rather than defaulted: a typo that quietly selects the shipped
    prompt would record a measurement under a prompt nobody chose.
    """
    try:
        return VARIANTS[prompt_id]
    except KeyError:
        known = ", ".join(sorted(VARIANTS))
        raise DomainError(f"no prompt named {prompt_id!r}. Known: {known}") from None
