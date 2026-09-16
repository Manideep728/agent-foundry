"""Whether the test that calls a real model can run on this machine.

Probed at collection, like the Docker image check, so the suite reports a skip
with a reason rather than a failure. CI has no GPU and no pulled model, so this
skip is the normal case there -- which is why nothing the adapter decides is
allowed to depend on a live server.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from control_plane.adapters.ollama_client import DEFAULT_BASE_URL

BASE_URL = os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")

_PROBE_TIMEOUT_S = 2.0
"""Short: this runs during collection, and a machine with no server must not
pay a long wait on every test run."""


def _model_available() -> bool:
    try:
        with urllib.request.urlopen(
            f"{BASE_URL.rstrip('/')}/api/tags", timeout=_PROBE_TIMEOUT_S
        ) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False
    models = body.get("models")
    if not isinstance(models, list):
        return False
    # A running server without the model is as unusable here as no server:
    # asking for it would pull several gigabytes mid-test-run.
    return any(entry.get("model") == MODEL for entry in models)


requires_ollama = pytest.mark.skipif(
    not _model_available(),
    reason=f"no ollama at {BASE_URL} serving {MODEL}; run `ollama pull {MODEL}`",
)
