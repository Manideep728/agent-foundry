"""The real clock, the real identifier source, and the build that is running.

Small enough to read in one sitting, and isolated here so the domain and the
services never import `time`, `uuid` or `subprocess` themselves.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from control_plane import __version__
from control_plane.domain.ids import RunId

_CHECKOUT = Path(__file__).resolve().parents[2]
"""The directory holding `control_plane/`, a git checkout in development."""

_DESCRIBE_TIMEOUT_S = 5.0


class SystemClock:
    def now_unix_ms(self) -> int:
        """Realtime. Orders runs. Never subtracted (D12)."""
        return time.time_ns() // 1_000_000

    def monotonic_ms(self) -> int:
        """Monotonic. Only ever subtracted. Cannot go backwards."""
        return time.monotonic_ns() // 1_000_000


class UuidIdGenerator:
    """Run identifiers as a date prefix plus random hex.

    The prefix makes a `runs/` listing readable in time order; the random part
    is what guarantees uniqueness. Nothing reads the date back out -- ordering
    comes from `created_at_unix_ms`, because deriving meaning from an
    identifier's shape would couple the domain to an adapter's formatting
    choice.
    """

    def new_run_id(self) -> RunId:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return RunId(f"{stamp}-{uuid.uuid4().hex[:8]}")


def harness_version(checkout: Path = _CHECKOUT) -> str | None:
    """The package version and the commit it was built from.

    `0.1.0+62540b902356`, or `0.1.0+62540b902356.dirty` when tracked files
    differ from that commit -- a run measured on uncommitted code names the
    commit it started from and says that it is not exactly that commit.

    None when there is no checkout to ask, as for an installed wheel. The
    package version alone is not returned in that case: it changes once per
    release, so it would name dozens of different builds with one string.
    """
    # Checked rather than left to git, which would walk up the tree and report
    # the commit of whatever repository happens to contain an installation.
    if not (checkout / ".git").exists():
        return None
    try:
        described = subprocess.run(
            # `--exclude '*'` ignores tags, so the answer is always the commit
            # itself rather than `v0.1.0-12-g...` on one machine and a bare hash
            # on another, depending on which tags were fetched.
            ["git", "describe", "--always", "--dirty", "--abbrev=12", "--exclude", "*"],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=_DESCRIBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = described.stdout.strip()
    if described.returncode != 0 or not commit:
        return None
    return f"{__version__}+{commit.replace('-dirty', '.dirty')}"
