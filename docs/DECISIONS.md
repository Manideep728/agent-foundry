# Architecture decisions

Each record states the context, the decision, the consequence, and what would
change it. Decisions are numbered and dated; superseded records are marked, not
deleted.

---

## D1 — The execution supervisor runs as PID 1 inside the validation container

*2026-09-11*

**Context.** Validation commands must be killable as a tree: a test suite that
spawns a server or a worker pool leaves children behind when only the direct
child is signalled. The supervisor can either run on the host and spawn
`docker run` as its child, or run inside the container.

**Decision.** The supervisor runs inside the container, as PID 1.

**Consequence.** A host-side supervisor's child is the `docker` CLI, not the
processes inside the container; those live in a separate PID namespace and are
not members of any host process group. Signalling the host process group
terminates the CLI and leaves the container running, so cleanup would require
`docker kill` and the process-group code would never do what it claims. Inside
the container the process tree is real.

Running as PID 1 carries obligations that the implementation must meet:
orphaned processes are reparented to it and must be reaped, and signals whose
disposition is `SIG_DFL` are discarded, so `SIGTERM` handling must be explicit.

The supervisor is therefore Linux-only. It depends on `setpgid`, `killpg`,
`waitpid`, `poll` and `signalfd`.

**What would change it.** A requirement to supervise processes that are not
containerised.

---

## D2 — Linux is the only supported target; macOS builds through Docker

*2026-09-11*

**Context.** Development happens on two machines: an Apple M1 and an x86-64
Windows machine running WSL2.

**Decision.** One source tree targeting Linux, with no platform conditionals.
The supervisor is built natively on Linux and inside Docker elsewhere.

**Consequence.** A second implementation for Windows would need `CreateProcess`
and Job Objects rather than process groups, and would never execute on the real
path, since the supervisor runs inside a Linux container regardless.

Measurement is affected by the split. The two machines differ in architecture
(arm64 against x86-64), container memory, and cgroup version (v1 against v2).
Latency and cost figures are therefore only meaningful from a named machine.
The supervisor records the architecture it ran on, and the control plane
refuses to admit a run into a benchmark table whose architecture does not match
the one configured for that benchmark. This is enforced in data rather than by
convention.

**What would change it.** Native Windows support becoming a requirement.

---

## D3 — The supervisor writes JSON but never parses it

*2026-09-11*

**Context.** The supervisor must accept a list of commands and emit a
structured result.

**Decision.** Input arrives as argv flags plus a newline-delimited command
file. Output is JSON produced by a hand-written serialiser. No JSON parser is
vendored.

**Consequence.** Writing JSON is a bounded problem — a fixed set of escapes,
control characters, integers, booleans and null — with no failure mode that
depends on input the program did not produce, and every branch reachable from a
unit test. Parsing is open-ended: nesting limits, surrogate pairs, number
grammar, duplicate keys, trailing data, and a decision for every malformed
input. Keeping the parser out of the C++ component removes that surface
entirely.

The input format is deliberately restricted: one command per line, blank lines
and `#` comments skipped, trailing carriage returns stripped. A command cannot
contain a literal newline; multi-line validation logic belongs in a script
inside the repository under test, invoked as a single command.

**What would change it.** The format acquiring structure. One additional field
per command is handled by moving to tab-separated values. A second dimension —
per-command environment maps, nested objects, arrays — is the point at which a
hand-written parser becomes worse than a dependency, and a parser should be
vendored instead.

---

## D4 — Results are returned as files on a bind mount

*2026-09-11*

**Context.** The control plane needs the structured result and the captured
output of each command.

**Decision.** The supervisor writes `result.json` and per-command log files to
a mounted output directory. Nothing is parsed out of the container's stdout.

**Consequence.** Container stdout carries supervisor diagnostics, any output
from processes that inherited the descriptors, and whatever the Docker log
driver adds; extracting a result from it would require framing and a recovery
path for when framing breaks. Validation output is also large — embedding a
test suite's output in JSON produces a file that is impractical to read. Log
files hold the output; the JSON holds counts, flags and a bounded excerpt from
the tail, where test runners put their summaries.

Absence of the result file is an unambiguous signal that the supervisor did not
finish, which a truncated stream is not.

The result file is written to a temporary name and renamed. Atomicity of that
rename on macOS bind mounts is not assumed: the control plane treats a missing,
truncated or unparseable result as a supervisor failure, preserves the raw
bytes, and cross-checks the container exit code.

**What would change it.** Streaming progress becoming a requirement, which
would need a separate channel rather than a change to this one.

---

## D5 — Validation commands run under `/bin/sh -c`

*2026-09-11*

**Context.** Validation commands are written by task authors and routinely use
shell features: directory changes, pipes, globs, and `&&` chains.

**Decision.** Each command is passed to `/bin/sh -c` as a single string.

**Consequence.** The alternative — building an argument vector — would require
the control plane to tokenise shell syntax, moving a parser into Python to
avoid one in C++.

The shell is an additional process between the supervisor and the program doing
the work, which is precisely the condition that makes process-tree termination
necessary rather than theoretical.

This interacts with testing in a way that is easy to get wrong. Some shells,
including `dash` and busybox, replace their own image when `sh -c` is given a
single simple command, so no additional process is created. A process-group
test written around `sh -c 'sleep 300'` can therefore pass without exercising
the behaviour it claims to test. Such tests must force a process layer with a
compound command.

**On command injection.** Commands originate from task definitions written by
the operator, not from a model. The untrusted artifact is the patch the agent
produces, and the boundary that contains it is the container — no network,
dropped capabilities, a process limit and a memory limit — not shell quoting.

---

## D6 — Model providers sit behind an interface owned by the domain layer

*2026-09-11*

**Context.** The project needs a real provider for meaningful results and an
offline path for tests.

**Decision.** One protocol, defined in the domain layer, with separate
implementations for a scripted fake, a locally hosted model, and a hosted API.
Service code never learns which implementation it holds.

**Consequence.** Tests and CI run against the fake with no network access.
Changing providers is configuration rather than restructuring.

Usage accounting is part of the interface from the start rather than added
later. Each implementation reports token counts, and cost is computed from a
per-provider price table; a locally hosted model reports tokens and a cost of
zero. Adding accounting afterwards would leave earlier runs with no token data
and nothing comparable to later ones.

Reports state which implementation produced each run. Results from different
providers are never combined into one aggregate.

---

## D7 — Benchmarks are split into two suites that are never combined

*2026-09-11*

**Context.** Hand-written fixtures are deterministic and fast but artificial.
Real repositories are credible but expensive to prepare.

**Decision.** Two suites. A synthetic suite of hand-written fixture
repositories, and a suite derived from an existing curated dataset of real
repository tasks. Each is reported separately.

**Consequence.** The expensive part of a real-repository task is not running it
but constructing it: identifying a defect, pinning a commit, and establishing
tests that fail before a fix and pass after. Existing datasets already carry
that information, which makes the second suite an adapter rather than an
authoring exercise, and allows comparison against published baselines.

Fixture identity is pinned by content hash, recorded in the task definition and
re-checked when the task is loaded. A mismatch refuses the run rather than
producing a result that cannot be reproduced.

**Status.** The synthetic suite is the committed scope. The derived suite is
conditional on verifying that its environments are available and that its task
schema maps cleanly; that verification has not been done.

---

## D11 — Storage is SQLite behind a repository interface

*2026-09-11*

**Context.** Runs must be durable and queryable, and the project must not
acquire operational dependencies before the core works.

**Decision.** SQLite is the source of truth for completed runs, reached through
an interface that does not expose SQL to callers.

**Consequence.** No service to run, and the database is a single file. Costs
are stored as integers in millionths of a unit rather than floating point, and
durations as integer milliseconds, so that stored values do not depend on
locale or floating-point formatting.

Each run records the image digest, supervisor version and architecture it ran
under. A run that cannot name the environment that produced it is not
reproducible.

**What would change it.** Concurrent writers, or a deployment where a single
file is not appropriate.

---

## D12 — Durations and timestamps come from different clocks

*2026-09-11*

**Context.** Both elapsed time and absolute time are recorded.

**Decision.** Durations are measured with a monotonic clock. Timestamps use the
realtime clock. A duration is never computed by subtracting two timestamps.

**Consequence.** The realtime clock is adjustable: NTP steps it, and a
virtual machine's guest clock resynchronises after the host sleeps, which can
move it by hours. A duration derived from it can be negative or absurd. The
monotonic clock cannot be set and is meaningless as an absolute time, so both
are needed for different purposes.

Host timestamps order runs; in-container monotonic deltas measure latency.
