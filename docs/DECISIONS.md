# Architecture decisions

Each record states the context, the decision, the consequence, and what would
change it. Decisions are numbered and dated; superseded records are marked, not
deleted. A number is never reused, so the sequence has gaps.

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

## D27 — Kafka fans out the expensive stage, and one collector keeps the database

*2026-09-21*

**Context.** `af bench` is a serial scheduler. It takes the database lock
(D17) and evaluates one task at a time. Each task spends seconds to minutes on
the agent and the validation, and milliseconds on writing its record.

**Options.** (a) A thread or process pool inside `af bench`. (b) A Kafka work
queue with a pool of workers. (c) Leave it serial.

**Decision: (b), as an optional extra.** At eight tasks on one machine, (a) is
the smaller answer, and it would be the right one if throughput were the only
goal. (b) was chosen because it puts the measurement behind at-least-once
delivery, rebalancing and worker loss, which are the failure modes a
distributed benchmark has to survive, and because the work already has a
fan-out shape. `af run` and `af bench` stay on the standard library and work
with no broker.

**Topology.**

- `agentfoundry-task-runs` has six partitions. The partition count is the limit
  on concurrent workers, so it is chosen, not defaulted. Topic auto-creation is
  off, because an auto-created topic has one partition, and a fan-out that does
  not fan out is worse than a missing topic. Messages were keyed by task; D37
  changed the key to the run.
- `agentfoundry-run-results` has one partition. One collector writes the
  database, so the single-writer rule of D17 holds. A second partition gives no
  parallelism and removes the total order that makes the collector's input
  replayable.

**The dispatcher sets the run identifier, not the worker.** Kafka delivers at
least once. A worker that finishes a run and stops before it commits the
offset gets the same message again, and so does the worker that takes its
partition. If each worker set its own identifier, a redelivery would store a
second run and inflate every rate. An identifier set at dispatch is the
idempotency key.

**The task body does not travel.** A task is pinned by a content hash and its
fixture is on disk. The message carries the hash that the dispatcher saw, and
a worker whose checkout hashes differently refuses the message. This applies
fixture immutability across the network, where the load-time check cannot see
the failure: both sides are consistent and hold two different versions of the
task.

**The configuration travels with the work.** The agent, provider, model,
prompt and image are the grouping key (D20). Without them on the message, a
pool started with two `--model` values produces one suite that mixes two
models, and no record says so. D28 changes how a worker uses them.

**Consequence.** A run can be measured twice: once by a worker that was
evicted or stopped, and again by the worker that took its partition. The
identifier keeps the counts correct; the cost is a wasted measurement. D29
measures this.

**What would change it.** A workload where the record write, not the agent or
the validation, is the slow stage.

## D28 — A worker measures under its own flags and refuses a message it cannot honour

*2026-09-28*

Amends D27, which reads the configuration from the message.

**Context.** A worker cannot build an agent from the message alone. The
Ollama URL, the solutions directory, the turn budget and the spend ceiling are
settings of a machine or a pool, not of a run.

**Decision.** A worker builds its agent from its own flags and compares the
resulting configuration with the message's. A worker that does not match
refuses the message, so a mixed pool still cannot produce a suite that looks
uniform.

| Case | Raised | Offset |
|---|---|---|
| bytes that no codec can read | `UndecodableMessage` | committed: no worker can read them, and an uncommitted poison message stops its partition permanently |
| another configuration, a task this checkout does not hold, a different fixture hash | `IncompatibleMessage` | not committed: the worker exits 2, and a worker with the correct configuration takes the message |
| any other error | not caught | not committed |

**Redelivery is answered from storage.** Each worker keeps its own database
and holds its lock. A message whose run is already stored in a final state is
republished, not measured again. This includes a run that startup recovery
marked interrupted, because an interrupted run is recorded and not retried.

**A newer schema is incompatible, not malformed.** Both codecs raise
`IncompatibleMessage` for a version above their own. A work message or a
result from a newer build stays on the topic for a newer consumer. A collector
that meets one stops with exit 2 until it is upgraded: a stopped collector
loses nothing, and a collector that committed past the message would lose the
measurement. An older or invalid version is malformed and is committed past.

**What would change it.** Agent settings that can be part of the message
without making the configuration specific to one machine.

## D29 — The poll interval stays at fifteen minutes, and an eviction is survivable

*2026-09-28*

Amended by D32.

**Context.** Kafka removes a consumer from its group when it holds one message
for longer than `max.poll.interval.ms`, and gives its partitions to another
consumer.

**Measured, by causing one.** A worker held a message for 60 s with the
interval at 45 s, the lowest value the client accepts. At 51.6 s the worker
left the group. At 51.7 s a second worker received the same run and measured
it again. At 66.6 s the first worker finished, published, and had its commit
refused with `UNKNOWN_MEMBER_ID`. The collector stored the first arrival and
reported the second as a conflicting duplicate. The identifier from D27 kept
the counts correct, and the cost was one wasted measurement.

**Handling time, from the stored runs:**

| Final state | Runs | Longest wall time |
|---|---|---|
| scored | 29 | 19.3 s |
| agent_error | 3 | 303.8 s, the 300 s client timeout |

**Decision.** Keep 900 s: three times the longest handling time measured. The
longest time is set by the model client's timeout, not by the task.

**Consequence.** The configured worst case is longer than the measured one.
The tool loop allows 12 turns of up to 300 s each, plus the validation
timeout. A run that used all of it would be evicted and measured twice. That
cost is bounded: a wasted run, a reported conflict and correct counts. A much
larger interval would let a hung worker hold six partitions for more than an
hour, which is worse.

**What would change it.** A longer client timeout, which D32 handles.

## D30 — Every run names its build, and a new message key changes the version

*2026-09-29*

**Decision.** Each run records `harness_version`: the package version, a
12-character commit, and `.dirty` when tracked files differ from that commit.
It is empty when the package is not in a git checkout, because the bare
version names many builds with one string. It is not part of the grouping key,
so a documentation commit does not split a set of repeats. `af bench` and
`af report` list the builds that a group covers.

**The message rule was wrong.** The decoders refuse unknown keys, so a new key
without a version change reached an older collector as a malformed message.
The collector committed past it, and the measurement was lost. Now any change
to a message's key set changes the version. An older consumer sees an
incompatible message and leaves it on the topic (D28). A newer consumer still
reads version 1, where the default of a missing key is honest. Each message is
checked against the key set of its own version, and encoding writes exactly
that set, so a decoded version-1 result encodes to the same bytes.

**Rejected.** A separate version for each message type. It is cleaner, but the
codecs already share one constant, and version-1 work messages decode
unchanged.

**What would change it.** A message format with explicit optional fields that
older consumers can skip.

## D31 — Every model call starts cold

*2026-09-29*

**Context.** Two tool-loop suites with the same model and prompt disagreed on
two tasks. Ollama reuses the prompt cache of an earlier request, so a reply
depended on the request and on the work the server had done before it.

**Options.** (a) Accept it, and publish ranges over repeats. (b) Record every
reply and replay it. (c) Remove the cause: unload the model before and after
every call, and refuse a reply that reports reused prompt tokens.

**Decision: (c).** It removes the cause instead of measuring around it, it can
be checked on every call, and it costs one model load per call, about 2.5 s
for a 7B model. Ranges over repeats are still reported, as a check on this
decision.

**Consequence.** Recorded agent time includes a model load for each call. A
second client of the same server can still load the model between the unload
and the request; the refusal makes that a failed run with its reason, never a
silent number. Workers that share one Ollama server therefore fail runs, so
each worker needs its own server. Determinism is shown for one GPU (RTX 4060),
one Ollama build and two models. Another GPU or build can compute a different
result, which is why every run records its environment and build.

**What would change it.** A server that can disable prompt-cache reuse per
request.

## D32 — The poll interval follows the model timeout

*2026-09-30*

Amends D29.

**Context.** D29 kept the poll interval at 900 s and found that the model
client's timeout sets the longest handling time. `--ollama-timeout` makes that
timeout a flag. `template-render` needs more than 300 s, and at 900 s one call
would hold the message for the whole interval.

**Decision.** A worker sets the interval to `max(900 s, 3 × timeout)`: the
ratio from D29, applied to the value that sets the bound. It never goes below
900 s, because that value was also measured against validation time, and a
short model timeout says nothing about validation.

**Consequence.** Unchanged from D29: in the worst case a tool loop can still
exceed the interval, and the cost stays one wasted measurement, a reported
duplicate and correct counts.

**What would change it.** A per-turn deadline in the tool loop, which would
give a tighter bound than the client timeout.

## D33 — PostgreSQL behind a repository port; SQLite stays the default

*2026-09-30*

**Context.** On ECS the collector's task and its instance can both be
replaced, and a SQLite file on that instance is lost with it. D11 said the
store could be replaced without touching the services, but the services
imported the SQLite repository directly, so that had not been tested.

**Options.** (a) PostgreSQL only. (b) An ORM over both stores. (c) A
repository protocol, a PostgreSQL adapter beside the SQLite one, and one shared
row mapping.

**Decision: (c).** `services/ports.py` defines the port from the methods the
services call. `adapters/run_rows.py` holds the column mapping, every
statement and the rehydration, so the two stores cannot disagree about what a
row means. Each adapter keeps its own DDL, transactions, schema version and
lock. `--db` takes a path or a `postgresql://` URL, and
`adapters/storage.open_repository` is the one place where either is opened and
locked. An ORM was rejected because it hides the SQL that the schema tests
read, and it adds a dependency to a core that uses only the standard library.

**Consequence.** Integer columns are `BIGINT`, because a millisecond timestamp
does not fit in 32 bits. Flags stay `0`/`1`, so one row builder serves both
stores. The D17 run lock becomes a session advisory lock, which the server
releases when the connection ends: the same property `flock` was chosen for.
One contract suite runs every repository test against both stores, and CI
runs the PostgreSQL half against a real server.

PostgreSQL is not here for concurrency; the collector is the only writer. It
is here for durability that does not depend on the machine that measured.

**What would change it.** More than one writer, or queries that the shared row
mapping cannot express.

## D34 — Run artifacts go to S3 before the result is published

*2026-09-30*

**Decision.** `af worker --artifacts s3://BUCKET/PREFIX` uploads a finished
run's directory, without the fixture copy, to `PREFIX/runs/RUN_ID/`, and then
publishes the result. The key comes from the run identifier, so no column and
no message key were added. When the upload fails, the worker does not publish
or commit, and exits 1. The redelivered message finds the run already stored,
and the republish path uploads before it publishes.

**Why this order.** "A result in the collector means its files are in S3" is a
rule a reader can depend on. If the worker published first, a stored result
could have files that never arrived. The upload is idempotent (the same keys
and the same bytes), which makes retry by redelivery safe, by the same
argument D27 makes for the result itself.

**Tested by** an in-process S3 fake (keys, exclusion, failure, idempotence),
worker tests with a recording store (order, failure, retry), and a real worker
against a local S3 server: 8 runs of 8 objects each.

**What would change it.** Artifacts too large to upload before the result is
due, which would need a separate completion marker.

## D35 — ECS on one EC2 instance, Kafka as a task, RDS for results

*2026-09-30*

**Context.** D1 puts the supervisor at PID 1 of its own container, and a
worker starts that container through the Docker socket. Fargate gives a task
no socket.

**Decision.** ECS with the EC2 launch type, on one instance, with host
networking:

- The worker mounts the Docker socket, and mounts the runs directory at the
  same path as on the instance, because the daemon resolves bind sources on
  the host. Workers run as an unprivileged user in the socket's group.
- The broker runs as an ECS task with the configuration of `broker.sh`. It
  advertises `localhost:9092`, which is correct for every task on the instance,
  and no listener faces outside. A managed Kafka cluster costs more per hour
  than everything else together, and is not what is being shown.
- The collector writes to RDS PostgreSQL (D33). RDS keeps the password in
  Secrets Manager, and ECS gives it to the task as `PGPASSWORD`, so it is never
  in Terraform state or a task definition.
- The default VPC, no NAT gateway, no inbound rules, SSM for a shell, and a
  budget alarm at $10 a month.

`infra/aws/smoke.sh` runs the whole lifecycle, including `terraform destroy`
and a check that nothing is left. Nothing in the code is region-specific: the
AMI comes from an SSM parameter and the region from `AF_REGION`.

**What would change it.** More than one instance, which needs a broker that
other instances can reach and a runs directory that is not on one host.

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


## D38 — Hosted models come through one OpenAI-compatible adapter, on free tiers

*2026-11-14*

**Context.** Every model in the benchmark ran on one 8 GiB GPU, so the
largest was 7B. A comparison between a small local model and a large hosted
one is the comparison worth making, and the project pays for no model calls.

**Options.** (a) One adapter per hosted provider, each with its SDK. (b) One
adapter for the OpenAI chat-completions API, which free tiers and local
servers such as llama.cpp and vLLM share. (c) Local models only.

**Decision: (b).** `adapters/openai_compat_client.py` uses the standard library
only, like the Ollama client, so the core still has no dependency. The run
records the host as its provider unless `--provider-name` says otherwise,
because the provider is part of the grouping key (D20). The key comes from the
variable `--api-key-env` names, and is never a flag, a record field or an
artifact.

**Rate limits.** A free tier answers 429 above its rate. The client waits as
`Retry-After` says, or 5 s doubling when it says nothing, for at most 6
attempts and 60 s per wait. The time it waits is reported apart from the
model's time:

- Each call's recorded latency excludes the wait.
- The run's agent note says how long the run waited.
- The run's agent-phase duration still includes the wait. A hosted run's
  agent time is therefore an upper bound on the model's time.

A column for the wait was rejected for this release. It changes both database
schemas and the message version (D30) for a figure that the note already
carries.

**Consequence.** A free tier costs nothing, so its runs record 0 micro-USD,
which is what was paid. Its tokens are real. Temperature 0 and a fixed seed
are sent, but a hosted model does not promise to be deterministic under them,
so repeats measure that rather than assume it. The prompts may be retained by
the provider under its free-tier terms, which is acceptable for synthetic and
open-source fixtures only.

**What would change it.** A paid run, which needs a price per model in place of
`TokenPrice.free`, or a provider whose API differs from chat completions.


## D39 — A real-repository task is a fixture taken from its image, plus a test patch

*2026-11-14*

Supersedes the plan in D24.

**Context.** D24 found that a SWE-bench task needs its own image, commands that
name the image's interpreter, and a test patch applied after the agent. It
planned to clone the source commit into the fixture position. A clean clone is
not what the image's environment was installed from: the install step writes
files into the source tree, such as version files and package metadata, and a
test run against a clean clone fails on import.

**Decision.** The fixture is the image's own source directory:

- `af fetch` pulls the instance image by digest, copies its `/testbed` out of
  a created container, and builds the validation image from it with the
  runner added (`infra/oss/Dockerfile`). The fixture goes to a cache outside
  the source tree, and the task pins its content hash as for any fixture.
- The workspace is mounted at the image's source path, so the installed
  environment imports the agent's edit.
- After the agent finishes and its patch is captured, the test patch is
  applied. First, each file the patch touches is restored to the fixture's
  version, as SWE-bench's own harness does. An agent that edited a graded test
  cannot change what it is graded on, and a patch that still does not apply
  makes the run `task_invalid`.
- The fail-to-pass and pass-to-pass tests become one command each, so a task
  is resolved only when all of its fail-to-pass tests pass, and regressed when
  any pass-to-pass test fails.

**Consequence.** Task identity stays a content hash with no schema change, as
D24 wanted. The fixture is as large as the repository, which is why it is not
committed. The local runner refuses these tasks, because it has no image.
Hosted CI does not pull the images, so the real-repository tests run locally.

**What would change it.** An image whose source directory is not its install
location, or a dataset whose tests are already in the source tree.
