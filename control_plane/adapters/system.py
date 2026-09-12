"""The real clock and the real identifier source.

Small enough to read in one sitting, and isolated here so the domain and the
services never import `time` or `uuid` themselves.
"""

from __future__ import annotations

import time
import uuid

from control_plane.domain.ids import RunId


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
