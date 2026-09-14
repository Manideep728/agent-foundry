#include "supervisor.hpp"

#include <poll.h>
#include <sys/wait.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <string_view>

#include "child.hpp"
#include "clock.hpp"
#include "outcome.hpp"
#include "sink.hpp"

namespace afrunner {
namespace {

// How long one poll waits before the loop looks around again. The timeout is
// never -1: a runner that can block forever cannot honour a deadline, and the
// deadline machine reduces this slice to whatever time is left.
constexpr int poll_slice_ms = 50;

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

    for (auto [sink, stream] :
         {std::pair{&out, "stdout"}, std::pair{&err, "stderr"}}) {
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

    // Drain before reaping, always. A Linux pipe holds about 64 KiB; waiting
    // in waitpid while the child fills it deadlocks both until the deadline
    // fires, and the command is then reported as a timeout it never had.
    while (streams[0].fd >= 0 || streams[1].fd >= 0) {
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

        const int ready = ::poll(fds.data(), count, poll_slice_ms);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
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

    int status = 0;
    while (::waitpid(child.pid, &status, 0) < 0 && errno == EINTR) {
        // Interrupted before the child was reaped; ask again.
    }

    out.close();
    err.close();

    result.verdict =
        decide_outcome(Facts{.spawned = true, .reaped = true, .status = status});
    result.out = out.capture();
    result.err = err.capture();
    result.duration_ms = monotonic_ms() - started;
    return run;
}

}  // namespace afrunner
