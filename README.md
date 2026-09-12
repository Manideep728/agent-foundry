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

Phase 3. One task runs end to end: the harness verifies a fixture against its
pinned content hash, copies it to an isolated workspace, invokes an agent,
captures the patch, executes the repository's own tests, scores the result and
stores it.

**The C++ execution supervisor and Docker isolation do not exist yet.**
Validation runs through `LocalRunner`, which executes commands directly on the
host with no isolation, no resource limits and no network restriction --
development only. Every run it produces records `pid1=false` and a runner
version naming it, so those rows are identifiable in the data rather than only
in this paragraph.

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
make; every target is a plain `uv` command.

## Run a task

```
$ uv run af run tasks/csv-quoted-fields.toml
af: warning: running with local-subprocess, which provides no isolation.
run 20260912-224928-bbecefa7  csv-quoted-fields  [synthetic]
  state       scored
  outcome     clean_patch
  resolved    yes         regressed   no
  fail->pass  1/1          pass->pass  1/1
  duration    agent 1ms   validation 81ms
  cost        0 micro-USD  (fake/scripted)
  patch       1391 bytes
  runner      local-subprocess 0.1.0 (no isolation)  pid1=False arch=arm64
```

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
- **No isolation today.** `LocalRunner` is not a sandbox. Point it only at
  fixtures whose contents are already trusted.

Benchmark numbers, when they exist, will name the machine that produced them
and be reported as ranges across runs. Results from different model providers
will not be averaged together.

## Licence

Not yet chosen.
