#!/bin/sh
# Start the single-broker development Kafka. Idempotent.
#
# Kafka 4.x is KRaft-only: the broker stores its own metadata in a Raft quorum
# and there is no ZooKeeper. One container plays both roles, which is why the
# process roles are `broker,controller` and the quorum has one voter, itself.
#
# A development broker, and deliberately nothing more: one replica of
# everything and PLAINTEXT. It is not a template for a deployed configuration.
set -eu

CONTAINER=${AF_KAFKA_CONTAINER:-af-kafka}
IMAGE=apache/kafka:4.1.0

wait_until_ready() {
    # Measured at about two seconds on this machine; thirty is a ceiling, not
    # an estimate. `kafka-topics.sh --list` succeeds only once the broker
    # answers metadata requests, which is what every caller needs.
    attempts=30
    until docker exec "$CONTAINER" /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:9092 --list >/dev/null 2>&1; do
        attempts=$((attempts - 1))
        if [ "$attempts" -eq 0 ]; then
            echo "broker: $CONTAINER did not become ready in 30s" >&2
            exit 1
        fi
        sleep 1
    done
}

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker start "$CONTAINER" >/dev/null
    wait_until_ready
    echo "broker: started existing $CONTAINER"
    exit 0
fi

# --restart unless-stopped: WSL stops an idle distro, and the Docker daemon
# with it. Without a restart policy the broker stays down afterwards, and the
# broker tests skip silently rather than fail.
#
# KAFKA_LOG_DIRS points at the volume. The image's default log directory is
# elsewhere, so a volume mounted without it holds nothing, and every topic is
# lost with the container.
#
# Replication factors are 1 because one broker cannot satisfy the default of 3,
# and the consumer group would never form.
#
# Auto-creation is off: an auto-created topic gets one partition, which caps a
# consumer group at one worker however many are started. A typo in a topic
# name must fail loudly instead.
#
# The initial rebalance delay stays at its three-second default, so several
# workers started together form one group rather than rebalancing after each.
docker run -d --name "$CONTAINER" --restart unless-stopped \
    -p 9092:9092 \
    -v af-kafka-data:/var/lib/kafka/data \
    -e KAFKA_NODE_ID=1 \
    -e KAFKA_PROCESS_ROLES=broker,controller \
    -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
    -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 \
    -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
    -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
    -e KAFKA_LOG_DIRS=/var/lib/kafka/data \
    -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
    -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=3000 \
    -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=false \
    "$IMAGE" >/dev/null
wait_until_ready
echo "broker: created $CONTAINER"
