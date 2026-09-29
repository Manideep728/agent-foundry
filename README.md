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

Eight tasks run end to end, in one command: the harness verifies a
fixture against its pinned content hash, copies it to an isolated workspace,
invokes an agent, captures the patch, executes the repository's own tests
inside a container, scores the result, stores it, and writes a report over
everything it has stored.

`afrunner` is the C++ execution supervisor. It reads a command file,
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

Every run records the environment it validated in: the architecture, whether
the supervisor was PID 1, and the content digest of the image. A run that
cannot name the environment that produced it is not reproducible, so a
benchmark table can require those fields rather than trusting them.

The database carries a schema version and upgrades itself in place when an
older one is opened. A database written by a newer build is refused rather than
read, because a field may have changed meaning and guessing produces plausible
wrong data.

Eight benchmark tasks exist, of deliberately varied difficulty and defect
shape. No benchmark results are published.

Design decisions are in [docs/DECISIONS.md](docs/DECISIONS.md). The interface
between the control plane and the supervisor is in
[docs/RUNNER_CONTRACT.md](docs/RUNNER_CONTRACT.md).

## Build and test

Requires [uv](https://docs.astral.sh/uv/). The Python toolchain is pinned to
3.12 by `.python-version`; uv fetches it if absent.

```
make sync     # create the virtualenv from pyproject.toml
make check    # lint, typecheck, tests
make test     # tests only
```

Without Docker and the image, the tests that start containers are skipped and
say so. CI runs two jobs: `make check`, and a runner job that builds the
supervisor and the image, runs the suite with the container tests active,
repeats those tests under `docker run --init`, and runs `make doctor`. One test
skips even there: the `ollama` marker asks a real local model to answer, and the
hosted runners have neither the server nor the pulled weights.

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

`make doctor` runs real validation containers and checks that this machine's
Docker enforces the limits the harness asks for. It exits 1 if any check fails.
On WSL2 with Docker Engine 29.1.3:

```
$ make doctor
PASS  runtime     server 29.1.3, cgroups v2, x86_64
PASS  image       afrunner:local: afrunner 0.1.0
PASS  pid1        pid1=True, shell /usr/bin/dash
PASS  network     1.1.1.1:53 blocked
PASS  memory      512 MiB under a 67108864-byte limit: kill detected
PASS  pids        --pids-limit 1: fork: Resource temporarily unavailable
INFO  bind        200 small files written to /work in 2 ms
```

## Run a task

Build the image first (`make runner-image`).

```
$ uv run af run tasks/csv-quoted-fields.toml
run 20260917-030423-1d5f524b  csv-quoted-fields  [synthetic]
  state       scored
  outcome     clean_patch
  resolved    yes         regressed   no
  fail->pass  1/1          pass->pass  1/1
  duration    agent 1ms   validation 1227ms
  cost        0 micro-USD  (fake/scripted)
  patch       1391 bytes
  runner      afrunner in docker image afrunner:local  pid1=True arch=x86_64
  image       sha256:fe2c76d56069c2a6ae482dca7c5da29dd2d77331a29389f31916d0c4e5c9824e
  workspace   runs/20260920-085144-004f6725
```

`--image` selects a different validation image. `--runner local` skips Docker
and prints a warning on every run. If Docker or the image is missing, the run
is recorded as `infrastructure_failed` and `af` exits 1.

One `af run` at a time uses a database. A second one started beside it exits 1
at once, because each run starts by recovering the runs it finds unfinished.

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

## Run a suite

`af bench` evaluates every task in a directory, defaulting to `tasks/`, and
summarises each suite separately.

```
$ uv run af bench
af: [1/8] cache-eviction
...
20260920-085212-1c350a5e  csv-quoted-fields        clean_patch
...

suite synthetic  8 task(s)  8 measured of 8 attempted
  config        fake/fake/scripted
  image         sha256:fe2c76d56069c2a6ae482dca7c5da29dd2d77331a29389f31916d0c4e5c9824e
  resolved      8/8
  regressed     0/8
                clean_patch              8
                patch_with_regressions   0
                tests_still_failing      0
                validation_timeout       0
                no_patch_produced        0
                malformed_output         0
  cost          0 micro-USD over 8 measured run(s)
  agent         min 0ms  median 0ms  max 1ms
  validation    min 856ms  median 914ms  max 1047ms
```

`config` names all three of the agent, the provider and the model, because a
summary covers exactly one of those and two of them do not identify it: the
single-shot agent and the tool loop drive the same model through the same
provider.

The whole suite holds one database lock, so nothing else may evaluate against
that database while it runs. It exits `0` only when **every** task produced a
measurement — a suite where the harness broke on one task exits `1`, so a
script never has to read the text to find out. What the agent scored does not
affect the exit code: eight runs that all produced no patch are eight
measurements.

Counts are printed as `passed/total` rather than percentages, and durations as
the range the runs covered rather than a mean — over eight tasks a mean is one
slow run away from meaningless, and every figure printed here is a duration
some run actually had.

Suites are summarised separately and never averaged together, and the model
that produced the runs is named. Runs that produced no measurement are listed
with the reason, and money spent by them is reported on its own line rather
than folded into the total: a failed run's cost is real, but it is not part of
what the benchmark cost to measure.

Interrupting a suite prints the summary for the tasks that finished and exits
`130`. The run that was in flight is recorded as aborted, which is kept
separate from a harness failure so that cancelling a suite does not look like
unreliability.

## Write a report

`af report` summarises every run in a database as Markdown, one section per
suite and configuration.

```
$ uv run af report --out report.md
wrote report.md
```

`--out -` writes to stdout instead. The report reads only and takes no run
lock, so one can be taken while a suite is running.

Each section names the configuration that produced it, then the counts, the
cost, the latency ranges, the environment that validated the runs, and a row
per task. A run that produced no measurement is listed with the task, the
failure and the reason, because a harness-failure count nobody can trace to a
cause cannot be acted on. Money spent by those runs is reported on its own line
and is not added to the measured total.

A run that had not finished when the report was written is named and excluded
from every figure, rather than quietly shrinking the denominator. The next
`af run` recovers it.

Two reports over the same database differ only where the measurements differ,
so a change between two benchmarks is a diff.

## Run distributed

A pool of workers can also measure a suite, over Kafka. `af dispatch`
publishes one message for each task. Each `af worker` measures the messages it
receives and publishes each record. `af collect` stores the records in one
database. Kafka is an optional extra: `af run` and `af bench` do not need it.

```
$ make kafka-up
sh infra/kafka/broker.sh
broker: started existing af-kafka
sh infra/kafka/topics.sh
...
$ uv sync --extra kafka
$ uv run af dispatch --agent fake
af: dispatched 8 run(s) for 8 task(s) to agentfoundry-task-runs at localhost:9092
20260929-044240-300587d1	cache-eviction
20260929-044240-c8d9cf11	csv-quoted-fields
...
$ uv run af worker --agent fake --worker-id w1 --db worker.sqlite3 --idle-exit-s 10
af: w1 waiting for work on agentfoundry-task-runs at localhost:9092
af: w1 handled 8 message(s)
20260929-044240-c8d9cf11  csv-quoted-fields        clean_patch
...
$ uv run af collect --db pool.sqlite3 --idle-exit-s 5
af: collecting from agentfoundry-run-results at localhost:9092 into pool.sqlite3
af: collected 8 stored, 0 duplicate, 0 conflicting-duplicate, 0 undecodable, 0 unfinished
...
$ uv run af report --db pool.sqlite3 --out -
```

Each process uses its own database. A worker keeps a local database for its
own recovery, and holds its lock, so two workers on one machine need two
`--db` paths. The collector database is the one that holds the results, and
`af report` reads it.

The dispatcher sets the run identifier. Kafka can deliver a message more than
once: for example, when a worker stops after it publishes a result and before
it commits the offset. The worker then republishes the stored record and does
not measure the task again. The collector stores the first arrival of each run
and reports a later arrival with different content.

A worker measures with its own flags. A message for a different agent, model,
prompt or image, or for a task this checkout does not hold, stops the worker
with exit `2`. The worker does not acknowledge the message, so a worker with
the correct configuration receives it. A message that cannot be decoded is
logged and acknowledged, because no worker can read it.

`make fanout-check` measures the suite two times with the fake agent: once with
`af bench`, and once with two workers in parallel and a collector. It exits
`0` only when the per-task results are identical and the collected runs are
exactly the dispatched runs.

## Agents

Three agents exist behind one interface, so they are comparable on the same
suite rather than each having its own.

Every run records which agent, provider and model produced it, and a summary
groups on all three. Two of them are not enough: the single-shot agent and the
tool loop drive the same model through the same provider, so a report keyed on
the provider and the model alone would merge the one comparison worth making
into a rate describing neither.

`--agent fake` is the default. It applies a known solution from
`tasks/solutions/` and calls no model, so the pipeline runs offline and in CI.

`--agent ollama` sends the task and the visible files to a local model in one
prompt and applies the files it sends back. It runs at temperature 0 with a
fixed seed. For the single-shot agent two runs of one task produce
byte-identical patches. For the tool loop they do not: two suites over the same
eight tasks, the same model and the same prompt gave the same totals and
disagreed on two tasks (see Limitations). Token counts are recorded and the cost is zero, which
is the honest figure for a model that cost nothing to call.

`--agent tools` gives the model three tools -- list the files, read one, write
one -- and a budget of turns, set by `--max-turns`. Each turn is recorded as
its own invocation, so the loop's cost is the sum of its calls rather than its
last one. It is not a replacement for the single-shot agent: the two answer the
same tasks through different amounts of context, and which is worth the tokens
is a measurement, not a preference.

The tests that decide a task's score can be listed as `hidden_paths` and are
then kept out of the agent's prompt, and out of what the tools will list or
read. They are still copied and still run; an agent shown the tests that grade
it is being handed the answer.

A reply the harness cannot read as an edit is scored `malformed_output` rather
than treated as an error. Failing to follow the output format is something a
model does, so it is measured like anything else it does. The reply is kept
next to the patch, with one line saying what was wrong with it, because a
malformed-output rate nobody can inspect cannot be acted on.

The system prompt is a named variant, chosen with `--prompt` and recorded on
every run. `tools-v1` is the prompt the first tool-loop measurements were taken
under; `tools-v2` is the default and is the one that produces patches. A
summary groups on the prompt, so a comparison between two of them is two rows
rather than one blended figure. A named variant is frozen: changing its text
fails a test, and the fix is a new name.

```
af run tasks/csv-quoted-fields.toml --agent ollama --model qwen2.5-coder:3b
af run tasks/template-render.toml --agent tools --max-turns 8
af bench --agent tools --model llama3.2:3b --prompt tools-v1
```

## Planned scope

The C++ supervisor, the Python control plane and CLI, Docker-isolated
validation, test-based scoring, SQLite storage with in-place schema upgrades,
eight benchmark tasks, three agents and Markdown report generation exist. The
first release also needs a hosted-provider adapter.

Distributed execution over Kafka exists on one machine. Automatic pull
requests and a web interface are outside the scope of the project.

## Limitations

- **Safety against hostile code.** Docker reduces accidental host access. It is
  not a hardened boundary against a deliberately malicious workload.
- **Statistical significance.** Single-trial results are single-trial results.
- **Portability.** The supervisor depends on POSIX process semantics and is
  Linux-only by design.
- **`--runner local` is not a sandbox.** Point it only at fixtures whose
  contents are already trusted.
- **`--agent tools` writes patches, and none of them is correct yet.** Under
  the prompt `tools-v2` and `llama3.2:3b`, five of seven measured tasks produce
  a patch and all five break a test that passed before. `resolved` is 0/7. The
  prompt fixed the mechanism, not the repair.
- **A tool-loop result is not reproducible at the task level.** Temperature is
  zero and the seed is fixed, and two suites over the same eight tasks, the
  same model and the same prompt still disagreed on two of them, while the
  totals matched. Read the per-task rows as one sample each.
- **The prompt is a measured variable, and it was tuned on this suite.** Every
  run records the named prompt it used, and a summary groups on it, so two
  prompts are two rows. The same eight tasks chose the prompt and then scored
  it, so the figure is an upper bound on an unseen task.
- **One task exceeds the client timeout.** `template-render` asks the model for
  a whole file and has not answered inside 300 s in three attempts. It is
  reported as `agent_error`, which produces no measurement, rather than as a
  model failure.
- **Tool calls do not always arrive on the tool channel.** `llama3.2:3b` uses
  the documented `tool_calls` field; both `qwen2.5-coder` models write the call
  into the message body instead, and the 3B one sends several JSON objects
  separated by newlines. The harness reads one JSON value written as text and
  refuses anything else, so those runs under-report what the model asked for.
  Guessing at a malformed call produces a plausible patch from a misread reply,
  which is worse than refusing it.

Benchmark numbers, when they exist, will name the machine that produced them
and be reported as ranges across runs. Results from different model providers
will not be averaged together.

## Licence

MIT. See [LICENSE](LICENSE).
