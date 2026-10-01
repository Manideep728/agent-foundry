#!/bin/sh
# Prove that fanning a suite out over Kafka preserves the measurement.
#
# The suite is measured twice with the fake agent, which is deterministic:
# once serially by `af bench`, and once by `af dispatch`, two `af worker`
# processes in parallel, and `af collect`. The per-task state, outcome,
# resolved and regressed flags must be identical, and the collected run ids
# must be exactly the dispatched ones -- no run lost, none counted twice.
#
# Needs a running broker with the topics created (`sh infra/kafka/topics.sh`)
# and the runner image (`make runner-image`).
#
# AF_COLLECT_DB chooses where the collector stores the results: a SQLite file
# in the working directory by default, or a `postgresql://` URL. A URL must
# name an empty database, because every run in it is compared with the
# dispatch.
#
# AF_WORKER_IMAGE runs the two workers as containers of the control-plane
# image (`make control-plane-image`) instead of as local processes: the shape
# a deployment has. Each mounts the host's Docker socket, to start the runner
# image beside itself, and the working directory at the same path it has here,
# because the daemon resolves the runner's bind mounts on the host.
set -eu

IMAGE=${AF_IMAGE:-afrunner:local}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/af-fanout-XXXXXX")
COLLECT_DB=${AF_COLLECT_DB:-$WORK/collected.sqlite3}
# `--no-sync`: two workers start at once, and two `uv run` calls that both
# sync the virtualenv can break each other.
AF="uv run --no-sync af"

echo "fanout-check: working in $WORK"

# Anything an earlier run left on the results topic would be counted here.
$AF collect --db "$WORK/drained.sqlite3" --idle-exit-s 5 >/dev/null 2>&1

$AF dispatch --agent fake --image "$IMAGE" >"$WORK/dispatched.tsv"

if [ -n "${AF_WORKER_IMAGE:-}" ]; then
    WORKER="docker run --rm --network host --user $(id -u):$(id -g)
        --group-add $(stat -c %g /var/run/docker.sock)
        -v /var/run/docker.sock:/var/run/docker.sock -v $WORK:$WORK
        $AF_WORKER_IMAGE"
else
    WORKER=$AF
fi

for name in w1 w2; do
    # Unquoted on purpose: $WORKER is a command and its arguments.
    $WORKER worker --agent fake --image "$IMAGE" --worker-id "$name" \
        --db "$WORK/$name.sqlite3" --runs "$WORK/$name-runs" \
        --idle-exit-s 15 >"$WORK/$name.out" 2>"$WORK/$name.err" &
done
wait

$AF collect --db "$COLLECT_DB" --idle-exit-s 5 >/dev/null
$AF bench --agent fake --image "$IMAGE" \
    --db "$WORK/bench.sqlite3" --runs "$WORK/bench-runs" >/dev/null 2>&1

uv run --no-sync python - "$WORK" "$COLLECT_DB" <<'PY'
import sys
from pathlib import Path

from control_plane.adapters.storage import display_name, open_repository

work = Path(sys.argv[1])
collect_db = sys.argv[2]


def stored(target: str) -> list:
    """Every run in a database, read the way `af report` reads it."""
    with open_repository(target, exclusive=False) as repository:
        return list(repository.list_runs(limit=None))


def results(runs: list) -> list[tuple[object, ...]]:
    return sorted(
        (
            str(r.run.task_id),
            r.run.state.value,
            r.outcome.value if r.outcome else None,
            r.score.resolved if r.score else None,
            r.score.regressed if r.score else None,
        )
        for r in runs
    )


dispatched = {line.split("\t")[0] for line in (work / "dispatched.tsv").open()}
collected_runs = stored(collect_db)
collected = {str(r.run.run_id) for r in collected_runs}
bench_runs = stored(str(work / "bench.sqlite3"))
split = {
    name: len((work / f"{name}.out").read_text().splitlines()) for name in ("w1", "w2")
}

failures = []
if collected != dispatched:
    failures.append(
        f"collected ids differ from dispatched: {len(dispatched - collected)} "
        f"missing, {len(collected - dispatched)} unexpected"
    )
if results(collected_runs) != results(bench_runs):
    failures.append("per-task results differ between fan-out and bench")

print(f"fanout-check: {len(dispatched)} dispatched, worker split {split}")
print(f"fanout-check: collected into {display_name(collect_db)}")
for failure in failures:
    print(f"fanout-check: FAILED: {failure}")
if failures:
    sys.exit(1)
print(f"fanout-check: identical to af bench over {len(bench_runs)} tasks")
PY
