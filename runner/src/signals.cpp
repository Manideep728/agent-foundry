#include "signals.hpp"

#include <signal.h>
#include <sys/prctl.h>
#include <sys/signalfd.h>
#include <sys/wait.h>

#include <cerrno>
#include <cstring>

namespace afrunner {
namespace {

sigset_t watched_signals() {
    sigset_t watched;
    ::sigemptyset(&watched);
    ::sigaddset(&watched, SIGTERM);
    ::sigaddset(&watched, SIGINT);
    ::sigaddset(&watched, SIGCHLD);
    return watched;
}

}  // namespace

std::string block_signals() {
    const sigset_t watched = watched_signals();
    if (::sigprocmask(SIG_BLOCK, &watched, nullptr) != 0) {
        return std::string("sigprocmask: ") + std::strerror(errno);
    }
    return {};
}

int make_signalfd() {
    const sigset_t watched = watched_signals();
    return ::signalfd(-1, &watched, SFD_NONBLOCK | SFD_CLOEXEC);
}

void become_subreaper() {
    // Failure is not worth reporting: inside the container the process really
    // is PID 1 and already receives orphans.
    ::prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0);
}

void AbortWatch::consume() {
    if (fd_ < 0) {
        return;
    }
    while (true) {
        signalfd_siginfo info{};
        const ssize_t got = ::read(fd_, &info, sizeof info);
        if (got != static_cast<ssize_t>(sizeof info)) {
            if (got < 0 && errno == EINTR) {
                continue;
            }
            return;
        }
        // SIGCHLD is watched only so the loop wakes promptly when a child
        // exits. It is not a request to stop.
        if (info.ssi_signo == SIGTERM || info.ssi_signo == SIGINT) {
            ++stop_requests_;
        }
    }
}

Reaped reap(pid_t child) {
    Reaped reaped;

    // A non-positive pid would turn this specific wait into waitpid's wildcard
    // or a process-group wait, which is the exact confusion this function
    // exists to prevent. There is no command in flight, so there is nothing to
    // attribute a status to.
    if (child > 0) {
        int status = 0;
        const pid_t got = ::waitpid(child, &status, WNOHANG);
        if (got == child) {
            reaped.child_reaped = true;
            reaped.child_status = status;
        }
    }

    while (true) {
        int orphan_status = 0;
        const pid_t orphan = ::waitpid(-1, &orphan_status, WNOHANG);
        if (orphan <= 0) {
            break;
        }
        if (orphan == child) {
            // It exited between the two calls. Keep the status rather than
            // counting the command as an orphan.
            reaped.child_reaped = true;
            reaped.child_status = orphan_status;
            continue;
        }
        ++reaped.orphans;
    }

    return reaped;
}

}  // namespace afrunner
