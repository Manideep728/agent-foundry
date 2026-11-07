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

Every run also records the harness build that measured it: the package version
and the commit, for example `0.1.0+62540b902356`. The suffix `.dirty` shows that
tracked files differed from that commit. The report lists each build that a
group of runs spans, so a reader can see when one group of numbers comes from
two versions of the scoring code.

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
exactly the dispatched runs. `AF_COLLECT_DB` sets the collector's database, and
CI runs the check once with SQLite and once with PostgreSQL.

`make control-plane-image` packages `af` as an image, built from the committed
tree, with the version of that commit recorded on every run it measures. A
worker in the image does not validate inside its own container. It starts the
runner image next to itself through the host's Docker socket, so the runner is
still PID 1 of its own container. The worker's runs directory must be mounted
at the same path as on the host, because the Docker daemon resolves the
runner's bind mounts on the host. `AF_WORKER_IMAGE=af-control-plane:local make
fanout-check` runs the two workers that way.

`af worker --artifacts s3://BUCKET/PREFIX` also stores each run's files in S3
under `PREFIX/runs/RUN_ID/`: the patch, the command file, `result.json`, the
logs and the fixture manifest. The workspace copy is left out, because the
fixture and the patch reproduce it. The worker uploads before it publishes the
result, so every result the collector stores has its files in the bucket. When
an upload fails, the worker stops without acknowledging the message. The next
worker that receives it finds the run already stored, uploads the files, and
publishes the result. The run is not measured again. The S3 client is the
`aws` extra.

## Store runs in PostgreSQL

`--db` takes a `postgresql://` URL in place of a file path, in every command.
SQLite stays the default. A collector that runs on a machine that can be
replaced needs a database that outlives that machine, and PostgreSQL is that
database. The driver is an optional extra.

```
$ make pg-up
sh infra/postgres/server.sh
postgres: created af-postgres
$ uv sync --extra postgres
$ uv run af bench tasks --db postgresql://agentfoundry@localhost:5432/agentfoundry
...
  resolved      8/8
  regressed     0/8
$ uv run af list --db postgresql://agentfoundry@localhost:5432/agentfoundry --limit 3
20261001-041056-1e886212  template-render          synthetic  clean_patch
20261001-041055-08ce5c57  semver-compare           synthetic  clean_patch
20261001-041054-fe8553ac  retry-backoff            synthetic  clean_patch
```

The two stores write the same rows with the same statements, and read them back
through the same code. One test suite holds both to one contract. The
differences are the ones a server needs:

- Every integer column is `BIGINT`, because a millisecond timestamp does not
  fit in 32 bits.
- The schema version is kept in a table, and the schema is created under an
  advisory lock, so two processes that open an empty database do not both
  create it.
- The run lock is a session advisory lock instead of a file lock, so it also
  holds between machines. The server releases it when the connection ends.

Give the password in `PGPASSWORD`, not in the URL. Output and reports show a
URL without its password, but a command line can still be seen by other
processes.

## Run on AWS

`infra/aws/` deploys the distributed path to one AWS account with Terraform:

- ECS on one EC2 container instance runs the broker, the collector and two
  workers. A worker starts the runner through the instance's Docker socket,
  and Fargate does not give a task that socket.
- RDS PostgreSQL holds the results. RDS keeps the password in Secrets Manager,
  and ECS gives it to the tasks as `PGPASSWORD`.
- S3 holds the run artifacts. ECR holds the two images.
- A budget sends an email at 50% of $10 a month.

`infra/aws/smoke.sh` builds the images from `HEAD`, deploys, dispatches the
suite two times with the fake agent, and stops one worker. Then it checks that
every dispatched run is in RDS exactly once and has its files in S3. At the
end it destroys the deployment and checks that nothing is left. It destroys
the deployment on a failure too, after it prints the task logs and the
instance's boot messages. Each wait has a time limit.

```
$ AF_REGION=us-east-2 AF_ALERT_EMAIL=you@example.com sh infra/aws/smoke.sh
...
== dispatch: every task x 2 (23:09:22Z)
af: dispatched 16 run(s) for 8 task(s) to agentfoundry-task-runs at localhost:9092
...
== waiting until all 16 runs are in RDS (23:09:43Z)
stored 16 of 16
every dispatched run is stored exactly once

== the report, read from RDS (23:09:58Z)
# AgentFoundry benchmark report
...
16 run(s) recorded, 16 of them measured, across 1 configuration(s).
...
| resolved | 16/16 |
| regressed | 0/16 |
...
- Architecture: x86_64
- Supervisor was PID 1: True
...
== artifacts in S3 (23:10:13Z)
artifacts for all 16 runs
...
== destroy (23:10:14Z)
Destroy complete! Resources: 36 destroyed.

== nothing left (23:13:29Z)
nothing left
smoke: exit 0
```

This run took 11 minutes: 5 for the deployment and 3 for the destroy. The fake
agent and one-second validations most likely finished the suite before the
worker stopped.
Thus this run does not show redelivery after a worker stops. The local
measurement in the next section shows it.

The default instance type is `m7i-flex.large`, because an account on the AWS
free plan can start only free-tier-eligible types. Set
`TF_VAR_instance_type` to change it.

## Throughput, worker loss and isolation cost

These figures come from one machine: 24 CPUs, WSL2, a local broker, the fake
agent, and real validation in Docker. Each trial measures 64 runs (8 tasks, 8
repeats) from the dispatch to the last stored result. The table shows the
median of three trials.

| Workers | Runs per minute | Speedup |
|---|---|---|
| 1 | 104 | 1.0x |
| 2 | 170 | 1.6x |
| 4 | 255 | 2.5x |

Four workers cannot share the six partitions of the work topic equally. With
twelve partitions, four workers measured 274 runs per minute (2.6x). The rest
of the difference from 4x is the dispatch and start time inside each trial,
and the one Docker daemon that every worker uses.

One trial stops a worker with `SIGKILL` after it has measured five runs. Kafka
gives the stopped worker's partitions to the other worker after the 45 s
session timeout. The other worker measures the remaining 59 runs, including
the run that the stopped worker was validating. The collector stores 64 of 64
runs, each one time.

Isolation cost, from 40 runs of each arm in turn, for the two commands of
`semver-compare`:

| Arm | p50 | p95 |
|---|---|---|
| Container, supervisor as PID 1 | 461 ms | 482 ms |
| Container, `/bin/sh` as PID 1 | 464 ms | 481 ms |
| Host, no container | 60 ms | 62 ms |

The supervisor adds no time that this measurement can find: the difference is
3 ms or less, and its sign changes. The container adds about 400 ms per
validation. For the command `true`, the container adds 195 ms, which is the
start and stop of the container alone.

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
fixed seed. Token counts are recorded and the cost is zero, which is the honest
figure for a model that cost nothing to call.

Every model call starts from an unloaded model. At temperature 0 a reply still
depends on how much of its prompt the server can reuse from an earlier request,
so the client unloads the model before and after each call. A reply that
reports reused prompt tokens anyway fails the run, because nothing could
reproduce it. Each call therefore includes a model load, about 2.5 s for a 7B
model, and the recorded agent time includes it.

Three suites of the tool loop, run back to back with `llama3.2:3b` and
`tools-v2`, agree on every task: the same outcome, the same patch hash, and the
same token counts on every turn. Each suite started after another client had
left the model loaded. Before the client started every call cold, two suites
under the same conditions disagreed on two tasks.

`af bench --repeats N` runs the whole suite N times in order. When any task ran
more than once, the summary and `af report` compare each task's runs: the
result, the patch, and the token count of every model call. A task agrees only
when all three match.

```
$ uv run af bench tasks --agent tools --model llama3.2:3b --prompt tools-v2 --repeats 3
```

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
PostgreSQL storage, eight benchmark tasks, three agents and Markdown report
generation exist. The
first release also needs a hosted-provider adapter.

Distributed execution over Kafka exists on one machine, and on AWS ECS with
RDS and S3. Automatic pull
requests and a web interface are outside the scope of the project.

## Limitations

- **Safety against hostile code.** Docker reduces accidental host access. It is
  not a hardened boundary against a deliberately malicious workload.
- **Statistical significance.** Eight tasks are a small sample. A result that
  repeats exactly on eight tasks still says little about a ninth task.
- **Portability.** The supervisor depends on POSIX process semantics and is
  Linux-only by design.
- **`--runner local` is not a sandbox.** Point it only at fixtures whose
  contents are already trusted.
- **`--agent tools` writes patches, and none of them is correct yet.** Under
  the prompt `tools-v2` and `llama3.2:3b`, four of seven measured tasks produce
  a patch and all four break a test that passed before. `resolved` is 0/7. The
  prompt fixed the mechanism, not the repair.
- **Reproducibility is shown on one machine.** The repeated suites ran on one
  GPU with one Ollama build. Another GPU or build can compute a different
  result, which is why every run records its environment and harness build.
  Two workers that share one Ollama server can load the model for each other,
  so their runs fail with that reason. Give each worker its own server.
- **The prompt is a measured variable, and it was tuned on this suite.** Every
  run records the named prompt it used, and a summary groups on it, so two
  prompts are two rows. The same eight tasks chose the prompt and then scored
  it, so the figure is an upper bound on an unseen task.
- **One task exceeds the client timeout.** `template-render` asks the model for
  a whole file and has not answered inside 300 s in six attempts. It is
  reported as `agent_error`, which produces no measurement, rather than as a
  model failure. `--ollama-timeout SECONDS` raises the bound; a worker's Kafka
  poll interval rises with it, so a long call is not mistaken for a dead worker.
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
