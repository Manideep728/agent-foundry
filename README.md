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

Phase 1, in progress. The decision record, the Python package skeleton and the
build tooling exist. **The execution supervisor, the agent pipeline and the
benchmark do not.** `af` currently exposes only `--version`.

Sections describing behaviour, commands and measurements are added as that
behaviour lands, not before.

Design decisions made so far are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Build and test

Requires [uv](https://docs.astral.sh/uv/). The Python toolchain is pinned to
3.12 by `.python-version`; uv fetches it if absent.

```
make sync     # create the virtualenv from pyproject.toml
make check    # lint, typecheck, tests -- everything CI runs
make test     # tests only
make run ARGS=--version
```

`make help` lists the documented targets. Nothing in the project requires
make; every target is a plain `uv` command.

## Scope

In scope for the first release: a C++ execution supervisor, a Python control
plane and CLI, Docker-isolated execution, one model provider plus a fake
adapter for offline tests, benchmark tasks with fixed fixtures, test-based
scoring, SQLite storage behind a repository interface, and a generated report.

Out of scope: Kafka, AWS, Terraform, PostgreSQL, a web dashboard, multiple
model providers, Kubernetes, distributed execution, automatic pull request
creation, and user accounts.

## What this project will not claim

- **Safety against hostile code.** Docker reduces accidental host access. It is
  not a hardened boundary against a deliberately malicious workload.
- **Statistical significance.** Single-trial results are single-trial results.
- **Portability.** The supervisor depends on POSIX process semantics and is
  Linux-only by design.

Benchmark numbers, when they exist, will name the machine that produced them
and be reported as ranges across runs. Results from different model providers
will not be averaged together.

## Licence

Not yet chosen.
