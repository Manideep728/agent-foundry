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

**Amendment, 2026-09-15.** The optimisation depends on the build of `dash`, not
on `dash` itself. Ubuntu 26.04's `dash` replaces its image for a simple command.
Debian bookworm's `dash`, which is `/bin/sh` in the validation image, forks the
command instead. The native tests and the container therefore see different
process trees for the same command line, and in the container every command has
a shell layer. D16 records the consequence for commands the memory limit kills.

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

**Amendment, 2026-09-15.** A run records its supervisor version, which names
the image tag, together with its architecture and `pid1`. It does not record the
image digest, so two images built under the same tag cannot be told apart in the
data. Recording the digest is open work. Concurrent writers on one database are
refused, as D17 describes.

**Amendment, 2026-09-16.** The digest is now recorded, and the architecture a
run reports is now the one it validated on. Two corrections:

The digest is read with `docker image inspect --format '{{.Id}}'` before the
container starts, rather than from the inspection that follows it, so that a run
which failed still names the image it failed in — those are the runs whose
provenance is most often asked for. The value is the local content address, not
a repository digest: the validation image is built locally and never pushed, so
it may carry no repository digest at all. A runner with no image records nothing
rather than a placeholder, so `image_digest IS NOT NULL` is a usable filter for
runs that were genuinely isolated. The column names the image the tag resolved
to immediately before the container started; rebuilding the image mid-run would
leave the record naming the older one, and passing the resolved identifier to
`docker run` in place of the tag would close that. It has not been done.

The architecture and `pid1` of a stored run were previously written once, before
any container existed, and never corrected — so every stored run claimed an
unknown architecture while the validation report beside it held the truth.

**Amendment, 2026-09-16.** The schema carries a version and upgrades in place.
An older database has each ordered step applied inside one transaction, so it
cannot come out holding half an upgrade and a version number claiming all of it.
A database written by a newer build is still refused rather than read. Refusing
an older one as well was rejected: it discards recorded measurements to avoid
writing a single `ALTER TABLE`.

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

---

## D13 — The contract's unstated behaviour, settled before the supervisor was written

*2026-09-13*

**Context.** `docs/RUNNER_CONTRACT.md` fixes the field names, the outcome
vocabulary and the exit codes, and a conformance test holds both languages to
them. It does not state ten behaviours an implementer must still choose, and a
silent choice on either side becomes a wrong measurement rather than an error.

**Decision.** Each is now written into the contract. The load-bearing ones:

- `path` is a bare name inside `--log-dir`. The document's own example
  contradicted its rule, and the reader resolves `log_dir / path`.
- The tail comes from a ring buffer over the whole stream, never from the log
  file. The file keeps the front, because it stops at `--max-log-bytes`, and
  the tail must be the end.
- A tail byte outside printable ASCII becomes one `?`. Newline and tab survive;
  carriage return does not. One byte for one byte keeps the tail inside
  `--tail-bytes`.
- A command file yielding no commands is exit 3, because a report with an empty
  `commands` array cannot be represented.
- The per-command budget is clamped to what remains of the total, so the total
  expiring inside a command is an ordinary deadline.
- `aborted` equals `exit_reason != completed`, and `orphans_reaped_total` is at
  least the sum of the per-command counts.
- Every write of the result names every command in the file, including the
  partial result written on exit 7.

**Consequence.** Two of these are load-bearing in a way that is easy to miss.

`killed_after_grace` is `escalated && deadline_fired`, never `escalated` alone.
An abort escalates to `SIGKILL` without any deadline having fired, and the
reader refuses a command that sets the flag without `deadline_signal_sent`. The
natural implementation wires one boolean to the field and produces a result the
control plane cannot parse, which surfaces weeks later as a missing measurement.

The command interrupted by an abort cannot be a timeout, so it is reported from
its wait status. `aborted` on the report is what tells the control plane to
refuse the whole run, not the outcome of that one command.

---

## D15 — The validation image carries the task's toolchain

*2026-09-14*

**Context.** The supervisor runs the task's own validation commands inside its
own container, so the container must hold every program those commands call.
The synthetic tasks validate with `python3 -m unittest`.

**Decision.** The image has two stages. A `debian:bookworm` stage compiles
`afrunner`. The runtime stage is `python:3.12-slim-bookworm` with the binary
copied in, and `afrunner` is its entrypoint in exec form. The runtime stays on
bookworm, like the builder, so the dynamically linked binary finds the
`libstdc++` it was built against.

**Consequence.** There is one image for each toolchain, not one image for
every task. A suite of real repositories needs its own images, each with the
same binary copied in from the build stage. The image binary comes from g++ 12,
while the native tests use the host compiler, so the container tests, not the
native suite, are what cover the binary that actually runs.

**What would change it.** Tasks whose toolchains cannot share a base image, or
a static build of `afrunner` that removes the need to match the distribution.

---

## D16 — A memory-limit kill is detected from outside the container

*2026-09-15. Superseded in part by D36: `docker inspect` is now the fallback.*

**Context.** The validation image's `/bin/sh` forks even a simple `sh -c`
command. When the memory limit kills a command, the shell survives and exits
137, so the runner reports an ordinary `exited 137`. That result is valid, and
it is scored as a failed test. The runner cannot tell it apart from a command
that exits 137 by itself.

**Decision.** The container is started without `--rm`. After it stops, the
control plane reads `State.OOMKilled` from `docker inspect`. When the flag is
true, the run is an infrastructure failure, whatever the exit code and the
result file say. The container is then removed on every path.

**Consequence.** The flag was checked by experiment on cgroups v2 before the
design was adopted. It was true for a killed child with and without a shell
layer, and under `docker run --init`, while the runner itself exited 0. It was
false for a command that exited 137 by itself. The control plane makes one more
Docker call per run, and a container that cannot be removed fails the run.

**What would change it.** A Docker or cgroup version where the flag reports only
a kill of PID 1. The test that runs an allocating command through the Docker
runner fails in that case.

---

## D17 — One `af run` at a time uses a database

*2026-09-15*

**Context.** Each `af run` starts by recovering the runs it finds unfinished:
it marks them interrupted and removes their containers. That is correct only
when no other process is still executing one of them. A second `af run` started
beside a live one was observed to mark the live run interrupted and remove its
container during validation.

**Decision.** `af run` takes an exclusive, non-blocking `flock` on a file beside
the database before recovery, and holds it until the run is stored. A second
`af run` on the same database exits 1 at once. Recovery also claims a run's
state before it cleans up, so a run that finishes in the gap keeps its result
and its container.

**Consequence.** The kernel releases the lock when its holder exits for any
reason, including `SIGKILL`, so a crashed run leaves no stale lock, and holding
the lock proves that no other `af run` is alive. Runs against one database are
serial. Parallel benchmarks need separate databases, or a scheduler that owns
the lock.

**What would change it.** A database on a network filesystem, where `flock`
semantics differ, or a need to run tasks in parallel from one process.

## D18 — The tests that grade a task are hidden from the agent

**Context.** A task's fixture contains the tests that decide its score. An
agent given the whole repository is given the answer, and a score measured that
way says more about reading comprehension than about repair. D7 commits the
second suite to SWE-bench Lite instances and to calibrating against published
baselines, and those baselines are produced without showing the model the tests
that grade it. Mixing the two conventions makes the numbers incomparable.

**Decision.** A task may list `hidden_paths` under `[fixture]`. Those files are
kept out of the agent's prompt. They are still copied into the workspace and
still run by validation: this governs prompts, not execution.

The list is explicit rather than derived. A command reads
`python3 -m unittest tests.test_quoting`, a module path, and turning that back
into a file is guesswork that breaks on the first task doing anything unusual.
A list is checked by reading it.

Only the fail-to-pass tests are hidden. A pass-to-pass test is a regression
check that already exists in the repository an agent would really be handed, so
hiding it would model a repository that never existed.

**Consequence.** An agent sees the problem statement, the source, and the tests
that were already there. It cannot read the specification it is being graded
against, which is the position a developer given a bug report is in.

**What would change it.** An oracle-setting benchmark, where showing the tests
is the point and the numbers are labelled accordingly.

---

## D19 — The real-repository suite is deferred, on integration cost

*2026-09-17*

**Context.** D7 made the second benchmark suite conditional on verifying that
its environments are available and that its task schema maps cleanly. That
verification had not been done.

**Decision.** Both claims are confirmed, and the suite is still deferred to a
later release.

**What the check found.** The dataset carries everything the task schema needs,
and its per-instance container images are published and usable: one was pulled
and run with the network disabled, its repository is present at exactly the
commit the dataset names, and a real test from that instance executes offline.
So availability and schema fit are not the obstacle.

The obstacle is that this harness identifies a task by the content hash of a
local fixture directory, which it copies into a workspace and diffs afterwards.
A real-repository task is a commit inside a prebuilt image, with no local
fixture at all. Adopting it means task identity becomes a commit, the patch
comes from version control rather than from a manifest comparison, and every
instance needs its own image carrying the supervisor. A further detail decides
it: those images ship without the test changes that expose the defect, so the
tests a task is graded on pass until the harness applies them — a step the
hand-written suite has no equivalent of.

**Consequence.** That is a change to the core model rather than an adapter
beside it, so it is scheduled as its own work rather than added to the current
release. The hand-written suite is unaffected and remains the committed scope.

**What would change it.** Nothing about the dataset, which is suitable. It
changes when the task model can express a task whose identity is a commit
rather than a content hash.

## D20 — A measurement is grouped by the agent, the provider and the model

A summary reports one row per suite and configuration, and a configuration is
all three of the agent, the provider and the model. None may be dropped.

Two of them are not enough. The single-shot agent and the tool loop drive the
same model through the same provider, so a key of provider and model alone
merges them — and they are the comparison the project keeps both agents to
make. Measured here, the same model resolves five of eight tasks single-shot
and none of them through the loop; blended, that is five of sixteen, a figure
true of neither.

The configuration is recorded on the run when the run is created, not derived
from the model calls it made. A run that failed on its way to the model made no
calls, and those are exactly the runs a reliability figure is about; a derived
key would file every one of them under no configuration at all. Where a run
predates the columns, the calls it did make still name the provider and the
model, and the strategy stays unrecorded rather than guessed.

## D21 — The report is Markdown, and reading a database takes no lock

`af report` renders every stored run as Markdown. Markdown because a report is
read where it is written and two reports over the same database differ only
where the measurements differ, so a change between benchmarks is a diff. HTML
needs a viewer and does not compare; JSON restates the database and leaves the
aggregation to whoever reads it.

Reporting acquires no run lock. The lock exists because an evaluation starts by
treating every unfinished run as abandoned, which is safe only while nothing
else evaluates. A reader changes nothing, and locking would prevent looking at
a long suite while it runs, which is when a report is most useful.

A run that has not finished is named and excluded from every figure. Counting
it would put a run with no result into a rate; dropping it in silence would
shrink the denominator whenever a process died, so the benchmark would look
better the more often it broke.

## D22 — A tool call written as text must be one JSON value

A model is expected to ask for a tool through the provider's `tool_calls`
field. Some chat templates put the call in the reply text instead, and the
harness reads that, because refusing it would measure the template rather than
the model. What it reads is narrow: the whole message must be one JSON object,
or one JSON array of them, optionally inside a code fence. Every entry must
name a tool that exists and carry an object of arguments. Anything else is
prose, and a message that mixes a call with a sentence is prose.

`qwen2.5-coder:3b` sends several JSON objects separated by newlines. That is
not one JSON value, and it stays prose. The rule that prose can never become an
action rests on one comparison, and a grammar widened to fit each template is
the parser for broken JSON that D3 refuses on the runner side.

The cost is real and is not hidden. Four genuine tool requests from that model
are recorded as a run that asked for nothing, so its stored outcomes are a
floor on what it intended. No figure taken from that model may be described as
the model producing nothing.

## D23 — A prompt is part of a measurement's identity

A run records the named prompt its agent sent, and a summary groups on it
beside the agent, the provider and the model. Two prompts are two rows.

The prompt moves a result further than the model choice does. Measured here,
one model under one prompt never edits a file, and under another writes patches
for five of seven tasks. A key that omitted the prompt would merge those into
one rate describing neither, which is the defect D20 exists to prevent.

A named variant is frozen once a run has recorded it, and a variant is the
system text together with the tool descriptions. A test pins a digest of each
one, so changing a shipped prompt fails the build and the fix is a new name.
Without that, a tuned description would change what the model reads while the
recorded name stayed the same, and the stored rows would be wrong rather than
incomplete.

## D24 — A deferred suite keeps a content hash, not a commit

The real-repository suite stays deferred. The reason is narrower than it was.

A task's identity does not have to become a commit. A task may name a source
commit, the harness may check that commit out into the fixture position, and
the existing content hash may then be taken over the result. `.git` is already
excluded from the hash and from the copy, so two clones of one commit still
agree, and `fixture_sha256` stays NOT NULL with no migration. That matters:
making it nullable needs a full table rebuild, which the migration rules
forbid.

What remains is the real cost, and it is not identity. Each instance needs its
own validation image with the runner inside it, the validation commands must
name the environment's interpreter rather than `python3`, and the failing tests
arrive in a separate patch that has to be applied after the agent has finished
and before validation starts. The harness already captures the patch before
validation, so the place to apply it is clean; the work around it is not.

## D36 — The runner counts memory-limit kills

*2026-11-06*

**Context.** D16 trusted `State.OOMKilled`. Docker sets that flag when it
receives an OOM event from containerd, and under CPU load the event is
sometimes lost. On a loaded machine the kernel counted the kill
(`oom_kill 1` in the cgroup's `memory.events`), the command exited 137, and
the flag stayed false permanently. A wait or a retry cannot correct it. The
test of a memory-limit kill failed in three of four CI jobs on 2026-11-06, and
in about one of every hundred trials under local CPU load.

**Options.** (a) Treat every command that exits 137 as a harness failure.
(b) Have the runner read the kernel's counter and write it beside
`result.json`. (c) Add the counter to `result.json`.

**Decision: (b).** The runner reads `oom_kill` from
`/sys/fs/cgroup/memory.events` after the last command and writes
`{"oom_kills": N}` to `--oom-result`. A nonzero count is a harness failure.
`docker inspect` answers only when the count is `null`. (a) is the option D16
rejected: it refuses every suite that exits 137 by itself. (c) changes the
domain report, the wire format and both database schemas for a value that no
stored report can hold, because a run with a nonzero count is never stored.

**Consequence.** The verdict comes from the kernel and not from an event, so
load cannot change it. A clean run on cgroups v2 makes one Docker call fewer.
The counter is readable only while the container exists, so only a process
inside the container can read it; that is the runner. The file is untrusted
like `result.json`, and the runner writes it after every command has ended, so
a planted copy is replaced.

**What would change it.** A Docker runtime without a private cgroup namespace.
The count is then `null` on every run, and the fallback is the D16 flag with
its weakness.


## D37 — Work is keyed by run, not by task

*2026-11-06*

**Context.** `af dispatch` keyed each work message by its task, so that a
re-dispatch of a task was consumed after the original. A throughput
measurement with the fake agent, 64 runs (8 tasks, 8 repeats) on a 24-CPU
machine, found that every repeat of a task shared one partition. Five of the
eight tasks hashed to one of the six partitions. With four workers, one worker
measured 40 runs and one measured none. Two workers gave 1.27 times the
throughput of one, and four workers gave 1.48 times.

**Options.** (a) Keep the task key and accept the imbalance. (b) Key by run
id. (c) Add partitions.

**Decision: (b).** The run is the unit of work, and a run id is unique, so the
hash spreads runs evenly. No consumer reads the order that the task key gave:
the worker, the collector and the report identify each run by its id. (c) does
not help, because eight keys still fill at most eight partitions, and the
hash can put several of them on one partition.

**Consequence.** Two dispatches of one task can be consumed in either order.
The repeats of one task can run on different workers, so a per-task range can
include more than one machine. Each run records its architecture, image and
harness build, so the report shows that mix. The partition count is still the
limit on concurrent workers.

**What would change it.** A consumer that needs the runs of one task in
dispatch order, for example a run that depends on the result of an earlier
run.
