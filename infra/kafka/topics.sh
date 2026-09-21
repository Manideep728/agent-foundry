#!/bin/sh
# Create the project's topics on a running broker. Idempotent.
#
# Topics are created here and never by the broker. Auto-creation is disabled in
# the compose file, because an auto-created topic gets one partition, and one
# partition caps a consumer group at one worker however many are started.
# Partition count is the parallelism ceiling and is chosen, not defaulted.
#
# Names are hyphenated throughout. Kafka warns on any topic name containing a
# '.' or a '_', because both become '_' in a metric name and two topics can
# collide there. Measured 2026-09-21: the warning fires for an all-dots name as
# readily as a mixed one, and only an all-hyphen name is silent. A warning
# printed on every start is a warning nobody reads.
set -eu

CONTAINER=${AF_KAFKA_CONTAINER:-af-kafka}
BOOTSTRAP=${AF_KAFKA_BOOTSTRAP_INTERNAL:-localhost:9092}

# Work items. Six partitions is the ceiling on concurrent workers; a validation
# run holds a Docker container and a model call, so this machine will not get
# near it, and raising the number later is cheap while lowering it is not.
TASK_RUNS_PARTITIONS=${AF_TASK_RUNS_PARTITIONS:-6}

# Finished records. Exactly one partition, deliberately. One collector owns the
# SQLite database -- the single-writer invariant the run lock already enforces
# -- so a second partition would buy no parallelism and would cost the total
# ordering that makes the collector's input replayable.
RUN_RESULTS_PARTITIONS=1

topic() {
	docker exec "$CONTAINER" /opt/kafka/bin/kafka-topics.sh \
		--bootstrap-server "$BOOTSTRAP" \
		--create --if-not-exists \
		--topic "$1" --partitions "$2" --replication-factor 1
}

topic agentfoundry-task-runs "$TASK_RUNS_PARTITIONS"
topic agentfoundry-run-results "$RUN_RESULTS_PARTITIONS"

docker exec "$CONTAINER" /opt/kafka/bin/kafka-topics.sh \
	--bootstrap-server "$BOOTSTRAP" --list
