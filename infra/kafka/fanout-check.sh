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
set -eu

IMAGE=${AF_IMAGE:-afrunner:local}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/af-fanout-XXXXXX")
# `--no-sync`: two workers start at once, and two `uv run` calls that both
# sync the virtualenv can break each other.
AF="uv run --no-sync af"

echo "fanout-check: working in $WORK"

# Anything an earlier run left on the results topic would be counted here.
$AF collect --db "$WORK/drained.sqlite3" --idle-exit-s 5 >/dev/null 2>&1

$AF dispatch --agent fake --image "$IMAGE" >"$WORK/dispatched.tsv"

for name in w1 w2; do
    $AF worker --agent fake --image "$IMAGE" --worker-id "$name" \
        --db "$WORK/$name.sqlite3" --runs "$WORK/$name-runs" \
        --idle-exit-s 15 >"$WORK/$name.out" 2>"$WORK/$name.err" &
done
wait

$AF collect --db "$WORK/collected.sqlite3" --idle-exit-s 5 >/dev/null
$AF bench --agent fake --image "$IMAGE" \
    --db "$WORK/bench.sqlite3" --runs "$WORK/bench-runs" >/dev/null 2>&1

uv run --no-sync python - "$WORK" <<'PY'
import sqlite3
import sys
from pathlib import Path

work = Path(sys.argv[1])
COLUMNS = "task_id, state, outcome, resolved, regressed"


def rows(name: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(work / f"{name}.sqlite3") as db:
        return db.execute(f"SELECT {COLUMNS} FROM runs ORDER BY task_id").fetchall()


dispatched = {line.split("\t")[0] for line in (work / "dispatched.tsv").open()}
with sqlite3.connect(work / "collected.sqlite3") as db:
    collected = {row[0] for row in db.execute("SELECT run_id FROM runs")}
split = {
    name: len((work / f"{name}.out").read_text().splitlines()) for name in ("w1", "w2")
}

failures = []
if collected != dispatched:
    failures.append(
        f"collected ids differ from dispatched: {len(dispatched - collected)} "
        f"missing, {len(collected - dispatched)} unexpected"
    )
if rows("collected") != rows("bench"):
    failures.append("per-task results differ between fan-out and bench")

print(f"fanout-check: {len(dispatched)} dispatched, worker split {split}")
for failure in failures:
    print(f"fanout-check: FAILED: {failure}")
if failures:
    sys.exit(1)
print(f"fanout-check: identical to af bench over {len(rows('bench'))} tasks")
PY
