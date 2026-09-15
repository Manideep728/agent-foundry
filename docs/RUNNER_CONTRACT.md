# Runner contract

The execution supervisor is a C++20 program that runs validation commands and reports what
happened. The control plane invokes it and reads its result. This document is the interface
between them.

It is written before either side is finished on purpose. Two programs in two languages that agree
on a schema only by reading each other's source will drift, and the drift is silent: it surfaces
as a wrong measurement rather than an error.

The Python side of this contract is `control_plane/domain/validation.py`, whose types mirror the
document field for field and enforce the invariants below in `__post_init__`.

Schema version: **1**.

## Invocation

```
afrunner --commands    /in/commands.txt
         --workdir     /work
         --result      /out/result.json
         --log-dir     /out/logs
         --timeout-ms  120000
         --total-timeout-ms 600000
         --grace-ms    5000
         --max-log-bytes 8388608
         --tail-bytes  4096
         --drain-after-exit-ms 2000
         [--stop-on-failure]
```

Flags are `--name value`, or bare for booleans. No clustering, no `=` form, no positional
arguments.

Everything is passed on the command line rather than through the environment, so the command line
is the run's provenance: one artifact to read when reproducing a run weeks later. The run record
does not store the `docker run` command line yet.

## Mounts

| Path | Mode | Contents |
|---|---|---|
| `/in` | read-only | `commands.txt` |
| `/out` | read-write | `result.json`, `logs/` |
| `/work` | read-write | the repository copy the agent patched |

`/in` is read-only because the patch under test is model-generated and executes with the same
privileges as the test suite. A `conftest.py` that could rewrite the command file could rewrite
the thing judging it.

## Command file

One command per line, in the order they run. Blank lines and lines whose first byte is `#` are
skipped. A trailing carriage return is stripped from every line.

The carriage-return rule is not hypothetical: a task file authored on Windows produces `\r\n`, and
`pytest -q tests/\r` fails with a confusing "no such file".

**A command cannot contain a newline.** The control plane rejects one at task-definition time,
because an embedded newline would silently split into two commands and every later command would
run under an index belonging to something else. Multi-line logic belongs in a script inside the
repository under test, invoked as one command.

A command's identity is its **zero-based index among non-skipped lines**. The runner does not
invent labels; the control plane wrote the file and already knows what each line is for.

Commands run as `/bin/sh -c "<line>"` with the working directory set to `--workdir`.

## Result

Written to `--result` as UTF-8 JSON: to a temporary name, `fsync`ed, then renamed.

Absence of the file is meaningful — it says the runner did not finish. A truncated stream would be
ambiguous; a missing file is not.

### Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | Refused by the reader unless known. |
| `runner_version` | string | |
| `arch` | string | From `uname()`. Recorded so a benchmark table can refuse a row produced on the wrong architecture, and so accidental emulation is visible. |
| `pid1` | bool | `getpid() == 1`. Makes the PID-1 obligations observable in data rather than assumed. |
| `shell` | string | Which `/bin/sh` actually ran the commands. |
| `workdir` | string | |
| `started_at_unix_ms` | int | Realtime. Orders runs. |
| `wall_ms` | int | Monotonic. Measures duration. |
| `exit_reason` | string | `completed` \| `total_timeout` \| `signal` |
| `aborted` | bool | The runner was cut short. |
| `orphans_reaped_total` | int | |
| `commands` | array | Indices exactly `0..n-1`, in order. |

### Per command

| Field | Type | Notes |
|---|---|---|
| `index` | int | Line number in the command file. |
| `command` | string | Verbatim, so the reader can detect a task edited between running and scoring. |
| `started_at_unix_ms` | int | |
| `duration_ms` | int | |
| `outcome` | string | `exited` \| `signaled` \| `timeout` \| `spawn_failed` \| `not_run` |
| `exit_code` | int \| null | Set **iff** `outcome` is `exited`. |
| `signal` | int \| null | Set **iff** `outcome` is `signaled` or `timeout`. |
| `timed_out` | bool | Equals `outcome == "timeout"`. |
| `deadline_signal_sent` | bool | The deadline fired, whatever happened next. |
| `killed_after_grace` | bool | The grace period elapsed and `SIGKILL` was sent. |
| `group_escaped` | bool | The process group did not die. |
| `drain_deadline_hit` | bool | Output was still arriving after the child was reaped. |
| `orphans_reaped` | int | |
| `spawn_error` | string \| null | Set **iff** `outcome` is `spawn_failed`. |
| `stdout`, `stderr` | object | See below. |

### Captured streams

| Field | Type | Notes |
|---|---|---|
| `bytes_total` | int | What the process produced. |
| `bytes_written` | int | What reached disk. Never exceeds `bytes_total`. |
| `truncated` | bool | Equals `bytes_written < bytes_total`. |
| `path` | string | A bare name inside `--log-dir`, such as `cmd-000.stdout.log`. Empty when the command never ran. |
| `tail` | string | The last `--tail-bytes`, ASCII-only. |

Output is capped, not cut off: on reaching `--max-log-bytes` the runner keeps reading and discards
the excess. Stopping the read would block the child on a full pipe and turn a chatty test into a
false timeout.

The log file therefore holds the **front** of the output while the tail holds the **end**, so the
tail cannot be read back out of the file. It comes from a ring buffer fed by every byte the runner
reads, which is why a truncated stream still reports the summary line rather than the collection
noise. A stream has a `path` if and only if the command actually ran, which is how "produced no
output" stays distinguishable from "never started".

The tail is the **end** of the output, because test runners put their summaries there. Bytes
outside printable ASCII are replaced with `?`, so a broken byte from a test cannot produce invalid
JSON. Newline and tab survive, because the summary is line-structured; carriage return does not,
because a progress bar that overwrites its own line would otherwise hide the summary behind its
last frame. One byte is replaced by one byte, so the tail never exceeds `--tail-bytes`.

`orphans_reaped_total` is at least the sum of the per-command counts and can exceed it. Orphans are
also collected between commands and during shutdown, when there is no command to attribute them to.
After the last command, the runner waits up to `--drain-after-exit-ms` for orphans that are still
exiting, and stops early when no child is left. It does not wait after a stop request. An orphan
that outlives that wait is not counted; in the container it ends with PID 1.

## Exit codes

**The exit code describes the runner, not the commands.** Whether validation passed is in
`result.json`; the exit code answers a different question — whether that file can be trusted.

| Code | Meaning |
|---|---|
| 0 | The runner finished and wrote a valid result. Commands may have failed. |
| 2 | Usage error: unknown flag, missing flag, unparseable number. |
| 3 | Command file unreadable, not a regular file, malformed, or holding no commands. |
| 4 | `--workdir` missing or not a directory. |
| 5 | Could not write the result file or log directory. |
| 6 | Internal error: `fork`, `pipe2`, `poll` or `signalfd` failed. |
| 7 | Aborted by a signal to the runner itself. A partial result is written where possible. |

`1` is deliberately unused, so a generic `1` from a wrapper script is distinguishable from a real
runner failure. `125`, `126` and `127` are unusable because Docker owns them.

A file of only blank lines and comments is exit 3 rather than a run of zero commands, because a
report with an empty `commands` array cannot be represented: the reader refuses it. Exhausting
`--total-timeout-ms` is **not** an error code. The runner finished and wrote a trustworthy result,
so it exits 0 and says `total_timeout` in the file.

**`137` must never be read as "the command failed."** It is `128 + 9` — `SIGKILL` — and the usual
cause is the container hitting its memory limit. Mapping it to a test failure would record the
host's memory ceiling as agent failures across an entire benchmark.

The same kill can also reach the runner inside a command. `/bin/sh` in the image forks even a
simple command, so when the memory limit kills the command's process, the shell exits `137` and the
runner reports `exited` with exit code `137`. The runner cannot tell that from a command that exits
137 by itself. The control plane can: after the container stops, it reads `State.OOMKilled` from
`docker inspect`, which is true when the memory limit killed any process in the container. A run
with that flag set is a harness failure whatever the result file says.

## Rules that decide what a result means

**The wait status is the authority for reporting; the deadline is the authority for scoring.**

A command can exit cleanly in the microseconds between its deadline firing and the signal landing.
The runner reports that honestly — `outcome: "exited"` with a real exit code — and sets
`deadline_signal_sent`. The control plane then declines to count it as a pass.

The reason is reproducibility, not fairness. Whether that command is reported as `exited(0)` or as
`timeout` is a race between the process and the signal, and it resolves differently on a fast
x86-64 machine than on an emulated arm64 VM. Scoring on the race would make the same run resolve
on one machine and not the other.

**`not_run` and `spawn_failed` are not failed tests.** A command that never ran cannot be said to
have failed; a command whose `fork` returned `EAGAIN` says something about the host's process
limit. The control plane treats both as harness failures, not as low scores.

## PID 1

The runner is PID 1 in the validation container, which carries two obligations.

**Orphans are reparented to it and must be reaped.** A double-forking test worker becomes PID 1's
child when its parent exits. Unreaped, those accumulate as zombies, consume PID slots against
`--pids-limit`, and make later commands fail to spawn.

The exit status of a command must come from `waitpid(child_pid, ...)`, never `waitpid(-1, ...)`.
As PID 1, `waitpid(-1)` can return an unrelated orphan, and attributing that orphan's exit code and
duration to the current command produces a wrong result with nothing appearing to go wrong. Orphans
are reaped separately with `WNOHANG` and counted into `orphans_reaped`.

**Signals whose disposition is `SIG_DFL` are discarded for PID 1.** A runner that installs no
handler ignores the `SIGTERM` from `docker stop`; Docker waits its full grace period, sends
`SIGKILL`, and the container dies without writing a result. `SIGTERM` and `SIGINT` are therefore
blocked with `sigprocmask` and consumed through a `signalfd` in the poll loop.

Between `fork` and `exec` the child must restore the signal mask and default dispositions. The
blocked mask is inherited across `exec`, and a test suite that inherits a blocked `SIGCHLD`
misbehaves in ways that look like flaky tests.

## Timeouts

On the deadline: `SIGTERM` to the process **group**, then `SIGCONT` (a stopped process cannot run
to handle `SIGTERM`), then `--grace-ms`, then `SIGKILL` to the group. Output continues to be
drained throughout — stopping the drain during the grace period turns it into a deadlock.

After `SIGKILL` the runner confirms the group is gone, bounded. If it never goes, `group_escaped`
is set and the run continues: `SIGKILL` cannot kill a process in uninterruptible sleep, and that
has to be reported rather than waited on forever.

`--total-timeout-ms` bounds the whole run. Ten commands at a ten-minute per-command timeout would
otherwise hold a container for over an hour. The per-command budget is clamped to whatever remains
of it, so the total expiring inside a command is an ordinary deadline with the ordinary escalation.

Cleanup deliberately outlives the total budget. The worst case is
`--total-timeout-ms` plus `--grace-ms` plus the bounded kill confirmation plus
`--drain-after-exit-ms` for the last command plus `--drain-after-exit-ms` for the orphans collected
at shutdown, so nothing may assert that `wall_ms` fits inside the total.

## Stopping the runner

`SIGTERM` or `SIGINT` stops the run: the group is signalled, the commands not yet reached stay
`not_run`, a partial result is written, and the runner exits 7. A second signal skips the grace
period. The whole path has to fit inside the stop grace period of whatever started the container,
which is ten seconds by default, so `--grace-ms` must be set well below that.

**An abort is not a deadline.** The command in flight has no deadline to report, so it is described
from its wait status, `deadline_signal_sent` stays false, and `killed_after_grace` stays false even
though the runner escalated. The reader refuses a command that sets `killed_after_grace` without
`deadline_signal_sent`, so the two are not interchangeable.

`--stop-on-failure` leaves every command after the first failure `not_run`, and the control plane
treats a `not_run` command as an incomplete validation rather than a low score. It is a debugging
aid, not a benchmark mode.

## What this contract does not provide

- **No guarantee against malicious code.** The container reduces accidental host access. It is not
  a hardened boundary, and nothing here should be described as one.
- **No portability.** The runner depends on POSIX process semantics — `setpgid`, `killpg`,
  `waitpid`, `signalfd` — and is Linux-only by design.
- **No atomicity claim for `result.json` on every filesystem.** The write is
  temp-file-plus-rename, which is atomic on ext4. On a macOS bind mount it is not assumed: the
  control plane treats a missing, truncated or unparseable result as a runner failure, preserves
  the raw bytes, and cross-checks the container exit code.

## Implementations

| Implementation | Isolation | Status |
|---|---|---|
| `afrunner` (C++20, in a container) | process groups, resource limits, no network | image from `make runner-image`; started by `DockerRunner`, the default for `af run` |
| `LocalRunner` (Python, `subprocess`) | **none** | development only, `af run --runner local` |

The image's entrypoint is `afrunner` in exec form, so it is PID 1 and receives the flags given to
`docker run` as its argv. The runtime base is `python:3.12-slim-bookworm`, because the validation
commands need the task's toolchain as well as the runner.

`LocalRunner` satisfies the reading half of this contract, so the pipeline runs without Docker. It
runs commands directly on the host with no isolation, no resource limits and no network
restriction, and it reports `pid1: false`. It signals the process group on a deadline, but that is
hygiene, not containment. It is not a security boundary and must never be pointed at a fixture
whose contents are not already trusted.
