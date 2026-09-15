# AgentFoundry

Claims about AI coding agents are usually unreproducible. A demo shows a patch
that worked once, on an unnamed repository, with no record of what the model
was asked, what it cost, how long it took, or what it broke elsewhere in the
codebase.

AgentFoundry is an execution and evaluation harness that makes those runs
measurable. It gives an agent a repository-level task, runs the agent against
an isolated copy of the repository, validates the resulting patch by executing
the repository's own tests, and records the patch, the captured output, the
exit status, wall-clock duration, token usage and estimated cost. The same task
run twice produces two comparable records.

## Status

Phase 5. One task runs end to end: the harness verifies a fixture against its
pinned content hash, copies it to an isolated workspace, invokes an agent,
captures the patch, executes the repository's own tests inside a container,
scores the result and stores it.

The C++ execution supervisor, `afrunner`, now exists. It reads a command file,
runs each line under `/bin/sh -c` in its own process group, captures both
streams to capped log files, enforces a per-command and a total deadline,
escalates from `SIGTERM` to `SIGKILL` after a grace period, reaps orphaned
descendants, and writes a `result.json` the control plane parses. A conformance
test runs the real binary and reads its output back through the Python types,
so the two languages cannot drift apart silently.

A container suite runs the image and checks the behaviour that exists only
when `afrunner` is PID 1: it reaps orphans that left the command's process
group, it records a fork refused by `--pids-limit` as `spawn_failed`, it
writes a partial result and exits 7 on `docker stop`, and it reports a command
killed by the memory limit. The suite also runs under `docker run --init`.

`make runner-image` packages `afrunner` as a container entrypoint, and `af run`
validates inside that container by default. The container has no network, no
capabilities, a read-only command file, and fixed memory, CPU and process
limits. When the memory limit kills a process, the run is recorded as an
infrastructure failure, not as a failed test. A run that the control plane did
not finish has its container removed the next time `af run` starts.

`--runner local` runs the commands directly on the host instead, with no
isolation, no resource limits and no network restriction -- development only.
Every run it produces records `pid1=false` and a runner version naming it, so
those rows are identifiable in the data rather than only in this paragraph.

One benchmark task exists. No benchmark results are published.

Design decisions are in [docs/DECISIONS.md](docs/DECISIONS.md). The interface
between the control plane and the supervisor is in
[docs/RUNNER_CONTRACT.md](docs/RUNNER_CONTRACT.md).

## Build and test

Requires [uv](https://docs.astral.sh/uv/). The Python toolchain is pinned to
3.12 by `.python-version`; uv fetches it if absent.

```
make sync     # create the virtualenv from pyproject.toml
make check    # lint, typecheck, tests -- everything CI runs
make test     # tests only
```

`make help` lists the documented targets. Nothing in the project requires
make; every target is a plain `uv`, `cmake` or `docker` command.

The C++ supervisor needs Linux, CMake 3.20+ and a C++20 compiler. The image
needs Docker.

```
make runner-test   # build afrunner and run its native test suite
make runner-image  # build the validation image, afrunner:local
```

The image's entrypoint is `afrunner`, so it runs as PID 1 in the container:

```
$ docker run --rm afrunner:local --version
afrunner 0.1.0
```

## Run a task

Build the image first (`make runner-image`).

```
$ uv run af run tasks/csv-quoted-fields.toml
run 20260915-070407-f7225053  csv-quoted-fields  [synthetic]
  state       scored
  outcome     clean_patch
  resolved    yes         regressed   no
  fail->pass  1/1          pass->pass  1/1
  duration    agent 2ms   validation 1121ms
  cost        0 micro-USD  (fake/scripted)
  patch       1391 bytes
  runner      afrunner in docker image afrunner:local  pid1=True arch=x86_64
```

`--image` selects a different validation image. `--runner local` skips Docker
and prints a warning on every run. If Docker or the image is missing, the run
is recorded as `infrastructure_failed` and `af` exits 1.

`af list` and `af show <run-id>` reprint stored runs. `af fixture-hash <dir>`
prints the digest a task file should pin.

Exit codes separate the two kinds of failure, so a script never parses this
output:

| Code | Meaning |
|---|---|
| 0 | a measurement exists, whatever it says about the agent |
| 1 | the harness failed; there is no measurement |
| 2 | usage error |
| 130 | aborted |

A run whose agent produced a patch that fails every test exits 0. The harness
did its job; the agent did not.

## Planned scope

The first release will include a C++ execution supervisor, a Python control
plane and CLI, Docker-isolated execution, a model-provider adapter, benchmark
tasks, test-based scoring, SQLite storage, and report generation.

Distributed execution, automatic pull requests, and a web interface are outside
the first release.

## Limitations

- **Safety against hostile code.** Docker reduces accidental host access. It is
  not a hardened boundary against a deliberately malicious workload.
- **Statistical significance.** Single-trial results are single-trial results.
- **Portability.** The supervisor depends on POSIX process semantics and is
  Linux-only by design.
- **`--runner local` is not a sandbox.** Point it only at fixtures whose
  contents are already trusted.

Benchmark numbers, when they exist, will name the machine that produced them
and be reported as ranges across runs. Results from different model providers
will not be averaged together.

## Licence

Not yet chosen.
