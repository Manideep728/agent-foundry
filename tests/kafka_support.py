"""Whether the tests that need a real broker can run on this machine.

The probe is a metadata request with a short timeout, not a produce. A produce
to a broker that is not there succeeds locally and fails much later, which
would make an absent broker look like a passing test.
"""

from __future__ import annotations

import os

import pytest

BOOTSTRAP = os.environ.get("AF_KAFKA_BOOTSTRAP", "localhost:9092")


def _broker_available() -> bool:
    try:
        from confluent_kafka.admin import AdminClient
    except ImportError:
        return False
    try:
        admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
        admin.list_topics(timeout=5)
    except Exception:
        return False
    return True


requires_broker = pytest.mark.skipif(
    not _broker_available(),
    reason=(
        f"no Kafka broker at {BOOTSTRAP}; run `sh infra/kafka/topics.sh` "
        f"against a started broker, and `uv sync --extra kafka`"
    ),
)
