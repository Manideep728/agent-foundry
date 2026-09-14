#include "child.hpp"

#include <gtest/gtest.h>

#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <string>

namespace {

using afrunner::Spawned;

std::string drain(int fd) {
    std::string text;
    char buffer[4096];
    while (true) {
        const ssize_t got = ::read(fd, buffer, sizeof buffer);
        if (got > 0) {
            text.append(buffer, static_cast<std::size_t>(got));
            continue;
        }
        if (got < 0 && errno == EINTR) {
            continue;
        }
        break;  // EOF, or EAGAIN with nothing left to read.
    }
    return text;
}

struct Ran {
    std::string out;
    std::string err;
    int status = 0;
    std::string error;
};

// Waits before draining, which is safe only because every command here writes
// far less than a pipe buffer. The supervisor must never do this: a command
// that fills the ~64 KiB buffer would block forever and be reported as a
// timeout. That is what the drain loop exists for.
Ran run(const std::string& command, const std::string& workdir = "/tmp") {
    Ran ran;
    Spawned child = afrunner::spawn(command, workdir);
    if (!child.error.empty()) {
        ran.error = child.error;
        return ran;
    }
    ::waitpid(child.pid, &ran.status, 0);
    ran.out = drain(child.stdout_fd);
    ran.err = drain(child.stderr_fd);
    ::close(child.stdout_fd);
    ::close(child.stderr_fd);
    return ran;
}

TEST(Child, CapturesStdout) {
    const Ran ran = run("echo hello");

    EXPECT_EQ(ran.error, "");
    EXPECT_EQ(ran.out, "hello\n");
    EXPECT_EQ(ran.err, "");
    ASSERT_TRUE(WIFEXITED(ran.status));
    EXPECT_EQ(WEXITSTATUS(ran.status), 0);
}

TEST(Child, CapturesStderrSeparately) {
    const Ran ran = run("echo out; echo err >&2");

    EXPECT_EQ(ran.out, "out\n");
    EXPECT_EQ(ran.err, "err\n");
}

TEST(Child, ReportsANonZeroExitCode) {
    const Ran ran = run("exit 42");

    ASSERT_TRUE(WIFEXITED(ran.status));
    EXPECT_EQ(WEXITSTATUS(ran.status), 42);
}

TEST(Child, RunsInTheRequestedDirectory) {
    std::string pattern = "/tmp/afrunner-child-XXXXXX";
    const char* made = ::mkdtemp(pattern.data());
    ASSERT_NE(made, nullptr);
    const std::string directory(made);

    const Ran ran = run("pwd", directory);
    EXPECT_EQ(ran.out, directory + "\n");

    ::rmdir(directory.c_str());
}

TEST(Child, ReportsAMissingWorkingDirectory) {
    const Ran ran = run("true", "/nonexistent-afrunner-workdir");

    EXPECT_NE(ran.error, "");
    EXPECT_NE(ran.error.find("chdir"), std::string::npos);
}

// The command is its own group leader, so a signal can reach a backgrounded
// grandchild. Checked while the child is alive, because a group stops existing
// once it is empty.
TEST(Child, IsItsOwnProcessGroupLeader) {
    Spawned child = afrunner::spawn("sleep 5", "/tmp");
    ASSERT_EQ(child.error, "");

    EXPECT_EQ(::getpgid(child.pid), child.pid);
    EXPECT_EQ(child.pgid, child.pid);
    EXPECT_NE(child.pid, ::getpgid(0)) << "the command must not share the runner's group";

    ::killpg(child.pgid, SIGKILL);
    int status = 0;
    ::waitpid(child.pid, &status, 0);
    ::close(child.stdout_fd);
    ::close(child.stderr_fd);
}

TEST(Child, ReadsStdinFromDevNull) {
    const Ran ran = run("readlink /proc/self/fd/0");

    EXPECT_EQ(ran.out, "/dev/null\n");
}

// O_CLOEXEC on every descriptor the runner opens. Without it a command could
// inherit a handle to result.json, and the patch under test executes with the
// same privileges as the test suite -- it could rewrite the thing judging it.
//
// The assertion names the file rather than counting descriptors. A count also
// sees whatever the process that launched the tests left open, which spawn has
// no control over: under WSL interop the harness inherits one.
TEST(Child, CannotInheritAFileTheRunnerHasOpen) {
    std::string pattern = "/tmp/afrunner-secret-XXXXXX";
    const int secret = ::mkstemp(pattern.data());
    ASSERT_GE(secret, 0);
    ASSERT_EQ(::fcntl(secret, F_SETFD, FD_CLOEXEC), 0);

    const Ran ran = run("ls -l /proc/self/fd");

    ::close(secret);
    ::unlink(pattern.c_str());

    EXPECT_EQ(ran.out.find(pattern), std::string::npos)
        << "the command can see the runner's open file:\n"
        << ran.out;
}

// A pipe reports EOF only when every write end is closed. If the parent kept
// its copy, the reader would never see EOF, and the supervisor would wait out
// the drain deadline on every single command instead of moving on.
TEST(Child, StdoutReachesRealEofOnceTheChildExits) {
    Spawned child = afrunner::spawn("echo done", "/tmp");
    ASSERT_EQ(child.error, "");

    int status = 0;
    ::waitpid(child.pid, &status, 0);

    std::string text;
    bool saw_eof = false;
    char buffer[256];
    for (int attempt = 0; attempt < 1000 && !saw_eof; ++attempt) {
        const ssize_t got = ::read(child.stdout_fd, buffer, sizeof buffer);
        if (got > 0) {
            text.append(buffer, static_cast<std::size_t>(got));
        } else if (got == 0) {
            saw_eof = true;
        } else if (errno == EAGAIN) {
            ::usleep(1000);
        } else if (errno != EINTR) {
            break;
        }
    }
    ::close(child.stdout_fd);
    ::close(child.stderr_fd);

    EXPECT_TRUE(saw_eof) << "no EOF: a write end of the pipe is still open";
    EXPECT_EQ(text, "done\n");
}

// The blocked mask survives exec, and a suite that inherits a blocked SIGCHLD
// misbehaves in ways that look like flaky tests.
TEST(Child, DoesNotInheritTheBlockedSignalMask) {
    sigset_t blocked;
    ::sigemptyset(&blocked);
    ::sigaddset(&blocked, SIGTERM);
    ::sigaddset(&blocked, SIGCHLD);
    sigset_t previous;
    ASSERT_EQ(::sigprocmask(SIG_BLOCK, &blocked, &previous), 0);

    const Ran ran = run("grep ^SigBlk: /proc/self/status");

    ::sigprocmask(SIG_SETMASK, &previous, nullptr);

    EXPECT_EQ(ran.out, "SigBlk:\t0000000000000000\n");
}

TEST(Child, DoesNotInheritIgnoredDispositions) {
    struct sigaction ignore {};
    ignore.sa_handler = SIG_IGN;
    ::sigemptyset(&ignore.sa_mask);
    struct sigaction previous {};
    ASSERT_EQ(::sigaction(SIGTERM, &ignore, &previous), 0);

    const Ran ran = run("grep ^SigIgn: /proc/self/status");

    ::sigaction(SIGTERM, &previous, nullptr);

    EXPECT_EQ(ran.out, "SigIgn:\t0000000000000000\n");
}

}  // namespace
