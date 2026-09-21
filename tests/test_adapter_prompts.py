"""A named prompt is frozen, the way a fixture is.

A fixture pins a content hash so that a task cannot change underneath the runs
that quote it. A prompt decides a measurement at least as much as the fixture
does -- `llama3.2:3b` goes from writing nothing to writing patches on the
system text alone -- so it is pinned the same way.

The digests below are the point of the file. Editing a shipped variant fails
this test, and the fix is a new id, never a new digest.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from control_plane.adapters.prompts import (
    DEFAULT_SINGLE_SHOT_PROMPT,
    DEFAULT_TOOL_PROMPT,
    VARIANTS,
    PromptVariant,
    variant,
)
from control_plane.domain.errors import DomainError

DIGESTS = {
    "tools-v1": "30344fe6d15bec46be4f1ae33be704669e9d8c13fb36d85b66e550f37af39584",
    "tools-v2": "62874a8db6d1c2e8fd486931f4926433fdfe9f1b9f61ca777b49a8b0722b8014",
    "single-shot-v1": (
        "6a5fc06d2e694c2421276b8659df6bf4059dd61402d7ecba1f9c9e7d0155ff5e"
    ),
}


def digest(spec: PromptVariant) -> str:
    """Everything the model reads, in a stable order.

    The tools are included because they are prompt text: a description tuned in
    place would otherwise change what a model sees while the recorded id stayed
    the same, which makes the database wrong rather than incomplete.
    """
    payload = json.dumps(
        {
            "system": spec.system,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in spec.tools
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("prompt_id", sorted(DIGESTS))
def test_every_prompt_variant_is_frozen_once_it_is_named(prompt_id: str) -> None:
    assert digest(VARIANTS[prompt_id]) == DIGESTS[prompt_id]


def test_every_named_variant_is_pinned() -> None:
    """A variant added without a digest would be free to change forever."""
    assert set(VARIANTS) == set(DIGESTS)


def test_an_unknown_prompt_is_refused_by_name() -> None:
    """A typo must not quietly select the shipped prompt.

    Defaulting here would record a run under a prompt nobody chose, which is
    the failure the TOML loader's unknown-key rule exists to prevent.
    """
    with pytest.raises(DomainError) as raised:
        variant("tools-v9")

    assert "tools-v9" in str(raised.value)
    assert "tools-v1" in str(raised.value), "the error must say what exists"


def test_the_defaults_name_variants_that_exist() -> None:
    assert VARIANTS[DEFAULT_TOOL_PROMPT].tools, "the loop's prompt must offer tools"
    assert not VARIANTS[DEFAULT_SINGLE_SHOT_PROMPT].tools


def test_the_two_loop_prompts_differ_only_in_their_system_text() -> None:
    """One factor at a time.

    If `tools-v2` also changed a tool description and then won, the measurement
    could not say which half won.
    """
    assert VARIANTS["tools-v1"].tools == VARIANTS["tools-v2"].tools
    assert VARIANTS["tools-v1"].system != VARIANTS["tools-v2"].system
