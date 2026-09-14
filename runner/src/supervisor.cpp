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

        std::array<pollfd, 2> fds{};
        std::array<Stream*, 2> watched{};
        nfds_t count = 0;
        for (Stream& stream : streams) {
            if (stream.fd < 0) {
                continue;
            }
            fds[count] = pollfd{stream.fd, POLLIN, 0};
            watched[count] = &stream;
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
        if (phase == Phase::Running && !child_reaped && now >= deadline) {
            deadline_fired = true;
            ::killpg(child.pgid, SIGTERM);
            // A stopped process cannot run to handle SIGTERM.
            ::killpg(child.pgid, SIGCONT);
            signal_sent = true;
            grace_deadline = now + options.grace_ms;
            phase = Phase::Grace;
        } else if (phase == Phase::Grace && !child_reaped && !escalated &&
                   now >= grace_deadline) {
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
    result.orphans_reaped = orphans;
    result.out = out.capture();
    result.err = err.capture();
    result.duration_ms = monotonic_ms() - started;
    return run;
}

}  // namespace afrunner
