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

Phase 0. The decision record and project scaffolding exist. **No application
code is implemented yet.** Sections describing behaviour, commands and
measurements are added as that behaviour lands, not before.

Design decisions made so far are in [docs/DECISIONS.md](docs/DECISIONS.md).

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
