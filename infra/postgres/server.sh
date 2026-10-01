#!/bin/sh
# Start the development PostgreSQL. Idempotent.
#
# A development server and nothing more: published on 127.0.0.1 only, with
# trust authentication, so no password exists to leak into a file or a URL.
# It is not a template for a deployed configuration; RDS is configured in
# infra/aws/.
#
#   AF_PG_URL=postgresql://agentfoundry@localhost:5432/agentfoundry
set -eu

CONTAINER=${AF_PG_CONTAINER:-af-postgres}
IMAGE=postgres:17.6
PORT=${AF_PG_PORT:-5432}

wait_until_ready() {
    # Asked over TCP, not the socket. A first start runs a temporary server to
    # initialise the data directory, listening on the socket only, and then
    # shuts it down; a socket check passes against that one and the caller's
    # first query meets "the database system is shutting down". Thirty seconds
    # is a ceiling.
    attempts=30
    until docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -U agentfoundry \
        -d agentfoundry >/dev/null 2>&1; do
        attempts=$((attempts - 1))
        if [ "$attempts" -eq 0 ]; then
            echo "postgres: $CONTAINER did not become ready in 30s" >&2
            exit 1
        fi
        sleep 1
    done
}

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker start "$CONTAINER" >/dev/null
    wait_until_ready
    echo "postgres: started existing $CONTAINER"
    exit 0
fi

# --restart unless-stopped for the reason broker.sh gives: WSL stops an idle
# distro, and without a restart policy the server stays down and the Postgres
# tests skip silently.
docker run -d --name "$CONTAINER" --restart unless-stopped \
    -p "127.0.0.1:$PORT:5432" \
    -v af-postgres-data:/var/lib/postgresql/data \
    -e POSTGRES_USER=agentfoundry \
    -e POSTGRES_DB=agentfoundry \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    "$IMAGE" >/dev/null
wait_until_ready
echo "postgres: created $CONTAINER"
