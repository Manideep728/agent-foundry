#include "child.hpp"

#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

namespace afrunner {
namespace {

enum Stage : int {
    stage_chdir = 1,
    stage_exec = 2,
};

// What the child tells the parent when it never reaches exec. Sent through a
// CLOEXEC pipe, so a successful exec closes it and the parent reads EOF.
struct ChildFailure {
    int stage;
    int error;
};

void close_if_open(int& fd) {
    if (fd >= 0) {
        ::close(fd);
        fd = -1;
    }
}

std::string describe(const ChildFailure& failure, const std::string& workdir) {
    const char* what = failure.stage == stage_chdir ? "chdir" : "exec";
    std::string message = std::string(what) + ": " + std::strerror(failure.error);
    if (failure.stage == stage_chdir) {
        message += ": " + workdir;
    }
    return message;
}

// Everything between fork and exec runs in a process that shares the parent's
// address space rules but only one thread. Only async-signal-safe calls are
// legal here, so every string is prepared before the fork and nothing
// allocates.
[[noreturn]] void run_child(char* const* argv, const char* workdir, int out_write,
                            int err_write, int status_write) {
    // Both sides of the fork call this. Child-only races into ESRCH on killpg
    // when the parent signals before the child has run; parent-only races into
    // EACCES once the child has exec'd, which would leave the command in the
    // runner's own group and turn a group signal on the runner itself.
    if (::setpgid(0, 0) != 0) {
        const ChildFailure failure{stage_exec, errno};
        [[maybe_unused]] const ssize_t sent =
            ::write(status_write, &failure, sizeof failure);
        ::_exit(127);
    }

    // The blocked mask survives exec. A test suite that inherits a blocked
    // SIGCHLD misbehaves in ways that look like flaky tests.
    sigset_t empty;
    ::sigemptyset(&empty);
    ::sigprocmask(SIG_SETMASK, &empty, nullptr);

    struct sigaction restore {};
    restore.sa_handler = SIG_DFL;
    ::sigemptyset(&restore.sa_mask);
    for (int signal_number = 1; signal_number < NSIG; ++signal_number) {
        if (signal_number == SIGKILL || signal_number == SIGSTOP) {
            continue;
        }
        ::sigaction(signal_number, &restore, nullptr);
    }

    // Never leave fd 0 closed: the child's next open() would land on it, and a
    // command that reads stdin would consume something it does not own.
    const int devnull = ::open("/dev/null", O_RDONLY);
    if (devnull >= 0) {
        ::dup2(devnull, STDIN_FILENO);
        ::close(devnull);
    }
    ::dup2(out_write, STDOUT_FILENO);
    ::dup2(err_write, STDERR_FILENO);

    if (::chdir(workdir) != 0) {
        const ChildFailure failure{stage_chdir, errno};
        [[maybe_unused]] const ssize_t sent =
            ::write(status_write, &failure, sizeof failure);
        ::_exit(127);
    }

    ::execv("/bin/sh", argv);

    const ChildFailure failure{stage_exec, errno};
    [[maybe_unused]] const ssize_t sent = ::write(status_write, &failure, sizeof failure);
    ::_exit(127);
}

}  // namespace

Spawned spawn(const std::string& command, const std::string& workdir) {
    Spawned result;

    std::array<int, 2> out{-1, -1};
    std::array<int, 2> err{-1, -1};
    std::array<int, 2> status{-1, -1};

    const auto abandon = [&](const char* what) {
        result.error = std::string(what) + ": " + std::strerror(errno);
        for (std::array<int, 2>* pipe : {&out, &err, &status}) {
            close_if_open((*pipe)[0]);
            close_if_open((*pipe)[1]);
        }
        return result;
    };

    // O_CLOEXEC on creation, so no fd leaks into the command. In particular the
    // command must never inherit a handle to result.json.
    if (::pipe2(out.data(), O_CLOEXEC) != 0) {
        return abandon("pipe2");
    }
    if (::pipe2(err.data(), O_CLOEXEC) != 0) {
        return abandon("pipe2");
    }
    if (::pipe2(status.data(), O_CLOEXEC) != 0) {
        return abandon("pipe2");
    }

    // Only the parent's read ends. Setting O_NONBLOCK through pipe2 would set
    // it on the write end the child inherits, and the child's write() would
    // fail EAGAIN instead of blocking, which silently loses output.
    if (::fcntl(out[0], F_SETFL, O_NONBLOCK) != 0 ||
        ::fcntl(err[0], F_SETFL, O_NONBLOCK) != 0) {
        return abandon("fcntl");
    }

    // Prepared before the fork: the child may not allocate.
    char shell_name[] = "sh";
    char shell_flag[] = "-c";
    std::string command_copy = command;
    std::string workdir_copy = workdir;
    char* const argv[] = {shell_name, shell_flag, command_copy.data(), nullptr};

    const pid_t pid = ::fork();
    if (pid < 0) {
        return abandon("fork");
    }
    if (pid == 0) {
        run_child(argv, workdir_copy.c_str(), out[1], err[1], status[1]);
    }

    // The other half of the setpgid race. The child makes the same call, and
    // whichever runs first wins; EACCES means the child already exec'd and
    // ESRCH that it already exited. Either way the group is settled, so the
    // result is deliberately ignored.
    [[maybe_unused]] const int grouped = ::setpgid(pid, pid);

    close_if_open(out[1]);
    close_if_open(err[1]);
    close_if_open(status[1]);

    // Reads EOF the moment exec succeeds, because the pipe is CLOEXEC.
    ChildFailure failure{};
    ssize_t got = 0;
    do {
        got = ::read(status[0], &failure, sizeof failure);
    } while (got < 0 && errno == EINTR);
    close_if_open(status[0]);

    if (got == static_cast<ssize_t>(sizeof failure)) {
        int discarded = 0;
        ::waitpid(pid, &discarded, 0);
        close_if_open(out[0]);
        close_if_open(err[0]);
        result.error = describe(failure, workdir);
        return result;
    }

    result.pid = pid;
    result.pgid = pid;
    result.stdout_fd = out[0];
    result.stderr_fd = err[0];
    return result;
}

}  // namespace afrunner
