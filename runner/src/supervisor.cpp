#include "supervisor.hpp"

#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <string_view>
#include <utility>

#include "child.hpp"
#include "clock.hpp"
#include "outcome.hpp"
#include "signals.hpp"
#include "sink.hpp"
#include "sysinfo.hpp"
#include "version.hpp"

namespace afrunner {
namespace {

// The longest one poll waits when no deadline is nearer. The timeout is never
// -1: a runner that can block forever cannot honour a deadline. Close to a
// deadline the wait shrinks to whatever time is left, so this bounds only how
// often the loop looks around when nothing is due.
constexpr int poll_slice_ms = 50;

// How long to wait for the group to disappear after SIGKILL before calling it
// escaped. Not a flag: SIGKILL cannot be refused, and the only cause of a
// survivor is uninterruptible sleep, which no timeout setting changes.
constexpr long long group_confirm_ms = 2000;

// How long one command waits for an orphan that became reapable as its drain
// loop ended. Not a flag, and deliberately small: the gap this closes is the
// microseconds between an orphan closing its pipe and the kernel making it
// waitable, not a wait for work still to be done. An orphan that is still
// running is left to the sweep at the end of the run, which is what
// --drain-after-exit-ms sizes.
constexpr long long post_command_reap_ms = 20;

enum class Phase {
    Running,
    Grace,
    Draining,
};

// Reads until the pipe has nothing more for now. Returns false at end of file.
//
// POLLHUP is not end of file. The writer can be gone while bytes are still
// buffered, so stopping at the hangup loses the last output of a crashing
// test, which is the part worth reading.
bool drain_ready(int fd, Sink& sink) {
    std::array<char, 65536> buffer{};
    while (true) {
        const ssize_t got = ::read(fd, buffer.data(), buffer.size());
        if (got > 0) {
            sink.feed(std::string_view(buffer.data(), static_cast<std::size_t>(got)));
            continue;
        }
        if (got == 0) {
            return false;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN) {
            return true;
        }
        return false;
    }
}

struct Stream {
    int fd;
    Sink* sink;
};

}  // namespace

CommandRun run_command(const Options& options, int index, const std::string& command) {
    AbortWatch never(-1);
    return run_command(options, index, command, never);
}

CommandRun run_command(const Options& options, int index, const std::string& command,
                       AbortWatch& watch) {
    CommandRun run;
    CommandResult& result = run.result;
    result.index = index;
    result.command = command;
    result.started_at_unix_ms = unix_ms();
    const long long started = monotonic_ms();

    Spawned child = spawn(command, options.workdir);
    if (!child.error.empty()) {
        result.verdict = decide_outcome(Facts{});
        result.spawn_error = child.error;
        result.duration_ms = monotonic_ms() - started;
        return run;
    }

    const auto tail_bytes = static_cast<std::size_t>(options.tail_bytes);
    Sink out(options.max_log_bytes, tail_bytes);
    Sink err(options.max_log_bytes, tail_bytes);

    for (auto [sink, stream] : {std::pair{&out, "stdout"}, std::pair{&err, "stderr"}}) {
        const std::string problem =
            sink->open(options.log_dir, log_basename(index, stream));
        if (!problem.empty()) {
            run.fatal = problem;
        }
    }

    std::array<Stream, 2> streams{
        Stream{child.stdout_fd, &out},
        Stream{child.stderr_fd, &err},
    };

    Phase phase = Phase::Running;
    const long long deadline = started + options.timeout_ms;
    long long grace_deadline = 0;
    long long confirm_deadline = 0;
    long long drain_deadline = 0;

    bool child_reaped = false;
    int status = 0;
    long long orphans = 0;
    bool deadline_fired = false;
    bool signal_sent = false;
    bool escalated = false;
    bool group_escaped = false;
    bool drain_deadline_hit = false;

    while (true) {
        long long due = poll_slice_ms;
        switch (phase) {
            case Phase::Running:
                due = deadline - monotonic_ms();
                break;
            case Phase::Grace:
                due = (escalated ? confirm_deadline : grace_deadline) - monotonic_ms();
                break;
            case Phase::Draining:
                due = drain_deadline - monotonic_ms();
                break;
        }
        const int timeout =
            static_cast<int>(std::clamp<long long>(due, 0, poll_slice_ms));

        std::array<pollfd, 3> fds{};
        std::array<Stream*, 3> watched{};
        nfds_t count = 0;
        for (Stream& stream : streams) {
            if (stream.fd < 0) {
                continue;
            }
            fds[count] = pollfd{stream.fd, POLLIN, 0};
            watched[count] = &stream;
            ++count;
        }
        if (watch.fd() >= 0) {
            fds[count] = pollfd{watch.fd(), POLLIN, 0};
            watched[count] = nullptr;
            ++count;
        }

        const int ready = ::poll(fds.data(), count, timeout);
        if (ready < 0 && errno != EINTR) {
            break;
        }

        // Drain first, always, and keep draining through the grace period.
        // A Linux pipe holds about 64 KiB; waiting in waitpid while the child
        // fills it deadlocks both until the deadline fires, and stopping the
        // drain while waiting for the group to die is the same deadlock again.
        if (ready > 0) {
            for (nfds_t i = 0; i < count; ++i) {
                if (fds[i].revents == 0) {
                    continue;
                }
                if (watched[i] == nullptr) {
                    watch.consume();
                    continue;
                }
                if (!drain_ready(watched[i]->fd, *watched[i]->sink)) {
                    ::close(watched[i]->fd);
                    watched[i]->fd = -1;
                }
            }
        }

        // Reap every iteration. signalfd coalesces signals, so counting SIGCHLD
        // would be wrong, and an unconditional WNOHANG cannot strand a zombie.
        // Unreaped zombies also keep a process group alive, which would turn
        // group_escaped into a false positive.
        const Reaped collected = reap(child.pid);
        orphans += collected.orphans;
        if (collected.child_reaped) {
            child_reaped = true;
            status = collected.child_status;
        }

        const long long now = monotonic_ms();

        // A command reaped in this same pass finished, whatever the clock says.
        // Firing the deadline on it would let a coarse poll slice turn a
        // command that completed in time into a timeout.
        if (phase == Phase::Running && !child_reaped &&
            (now >= deadline || watch.requested())) {
            // An abort is not a deadline. Leaving deadline_fired false is what
            // keeps killed_after_grace false, and the reader refuses a report
            // that sets that flag without the deadline having fired.
            deadline_fired = now >= deadline;
            ::killpg(child.pgid, SIGTERM);
            // A stopped process cannot run to handle SIGTERM.
            ::killpg(child.pgid, SIGCONT);
            signal_sent = true;
            grace_deadline = now + (watch.immediate() ? 0 : options.grace_ms);
            phase = Phase::Grace;
        } else if (phase == Phase::Grace && !child_reaped && !escalated &&
                   (now >= grace_deadline || watch.immediate())) {
            ::killpg(child.pgid, SIGKILL);
            escalated = true;
            confirm_deadline = now + group_confirm_ms;
        } else if (phase == Phase::Grace && !child_reaped && escalated &&
                   now >= confirm_deadline && ::kill(-child.pgid, 0) == 0) {
            // SIGKILL cannot kill a process in uninterruptible sleep. Report it
            // rather than waiting on it forever.
            group_escaped = true;
            break;
        }

        if (child_reaped && phase != Phase::Draining) {
            drain_deadline = now + options.drain_after_exit_ms;
            phase = Phase::Draining;
        }

        if (phase == Phase::Draining) {
            if (streams[0].fd < 0 && streams[1].fd < 0) {
                break;
            }
            // A leaked grandchild holding the write end open means end of file
            // never arrives, so the wait for it has to be bounded.
            if (monotonic_ms() >= drain_deadline) {
                drain_deadline_hit = true;
                break;
            }
        }
    }

    for (Stream& stream : streams) {
        if (stream.fd >= 0) {
            ::close(stream.fd);
            stream.fd = -1;
        }
    }
    out.close();
    err.close();

    result.verdict = decide_outcome(Facts{
        .spawned = true,
        .reaped = child_reaped,
        .status = status,
        .deadline_fired = deadline_fired,
        .signal_sent = signal_sent,
        .escalated = escalated,
    });
    if (group_escaped) {
        result.verdict.group_escaped = true;
    }
    result.drain_deadline_hit = drain_deadline_hit;
    result.out = out.capture();
    result.err = err.capture();
    result.duration_ms = monotonic_ms() - started;

    // The drain loop reaps only while this command holds its pipes open, so an
    // orphan that becomes reapable in the instant after the loop ends has no
    // later pass within the command. Left unreaped it is a zombie that holds a
    // PID slot against --pids-limit, and the next command fails to spawn for a
    // reason that has nothing to do with it. Taken after duration_ms is read,
    // so this cleanup never inflates the command's own measurement. A stop
    // request skips the wait: the caller's grace period is already running.
    orphans += reap_remaining_orphans(watch.requested() ? 0 : post_command_reap_ms, watch);
    result.orphans_reaped = orphans;
    return run;
}

RunAll run_all(const Options& options, const std::vector<std::string>& commands) {
    AbortWatch never(-1);
    return run_all(options, commands, never);
}

RunAll run_all(const Options& options, const std::vector<std::string>& commands,
               AbortWatch& watch) {
    RunAll all;
    Report& report = all.report;

    report.runner_version = std::string(runner_version);
    report.arch = arch();
    report.pid1 = is_pid1();
    report.shell = shell_path();
    report.workdir = options.workdir;
    report.started_at_unix_ms = unix_ms();

    const long long started = monotonic_ms();
    const long long total_deadline = started + options.total_timeout_ms;

    // Every command gets an entry before any of them runs. The reader requires
    // indices 0..n-1 exactly, so a run that stops early is unparseable unless
    // the unreached entries are already there. Each is overwritten in place.
    report.commands.reserve(commands.size());
    for (std::size_t i = 0; i < commands.size(); ++i) {
        CommandResult placeholder;
        placeholder.index = static_cast<int>(i);
        placeholder.command = commands[i];
        placeholder.started_at_unix_ms = report.started_at_unix_ms;
        placeholder.verdict = not_run();
        report.commands.push_back(std::move(placeholder));
    }

    for (std::size_t i = 0; i < commands.size(); ++i) {
        // Nothing polls the descriptor between commands, so look once here.
        // Otherwise a stop arriving in the gap waits for the next command to
        // finish before it is noticed.
        watch.consume();
        if (watch.requested()) {
            report.exit_reason = ExitReason::Signal;
            break;
        }

        const long long remaining = total_deadline - monotonic_ms();
        if (remaining <= 0) {
            // Ten commands at a ten-minute deadline would otherwise hold a
            // container for over an hour. The rest stay not_run.
            report.exit_reason = ExitReason::TotalTimeout;
            break;
        }

        // Folding the total budget into the per-command deadline means the loop
        // only ever tracks one deadline, and the total expiring mid-command is
        // an ordinary timeout with the ordinary escalation.
        Options budgeted = options;
        budgeted.timeout_ms = std::min(options.timeout_ms, remaining);

        CommandRun run = run_command(budgeted, static_cast<int>(i), commands[i], watch);
        report.orphans_reaped_total += run.result.orphans_reaped;
        report.commands[i] = std::move(run.result);
        if (!run.fatal.empty()) {
            all.fatal = run.fatal;
            return all;
        }

        if (watch.requested()) {
            // The remaining entries stay not_run, and the report says the run
            // was cut short rather than completed.
            report.exit_reason = ExitReason::Signal;
            break;
        }

        if (options.stop_on_failure && !succeeded(report.commands[i].verdict)) {
            // A truncation the caller asked for is not the runner being cut
            // short, so exit_reason stays completed and aborted stays false.
            break;
        }
    }

    // Each command already sweeps the instant after its own drain loop, so what
    // reaches here is an orphan that was still running when the last command
    // ended. This is the wait --drain-after-exit-ms sizes. A stop request skips
    // it, because the caller's own grace period is already running.
    report.orphans_reaped_total +=
        reap_remaining_orphans(watch.requested() ? 0 : options.drain_after_exit_ms, watch);

    report.wall_ms = monotonic_ms() - started;
    return all;
}

}  // namespace afrunner
