#include "signals.hpp"

#include <gtest/gtest.h>

#include <signal.h>
#include <sys/signalfd.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>

namespace {

// Without the block this call terminates the test binary. Consuming the signal
// through a descriptor instead of a handler is what lets the runner answer
// `docker stop` by writing a result rather than dying at SIG_DFL.
TEST(Signals, ABlockedSignalArrivesOnADescriptorInsteadOfKilling) {
    ASSERT_EQ(afrunner::block_signals(), "");
    const int fd = afrunner::make_signalfd();
    ASSERT_GE(fd, 0);

    ASSERT_EQ(::raise(SIGTERM), 0);

    signalfd_siginfo info{};
    ssize_t got = 0;
    for (int attempt = 0; attempt < 1000; ++attempt) {
        got = ::read(fd, &info, sizeof info);
        if (got == static_cast<ssize_t>(sizeof info)) {
            break;
        }
        ::usleep(1000);
    }
    ::close(fd);

    ASSERT_EQ(got, static_cast<ssize_t>(sizeof info));
    EXPECT_EQ(info.ssi_signo, static_cast<std::uint32_t>(SIGTERM));
}

// The misattribution guard. waitpid(-1) can return an unrelated orphan, and
// recording its exit code as the command's produces a wrong measurement with
// nothing appearing to go wrong. The two exit codes here are deliberately
// distinctive so a failure reads clearly.
TEST(Signals, ReapKeepsTheCommandsStatusApartFromAnOrphans) {
    afrunner::become_subreaper();

    // The command outlives the orphan on purpose. If both exited at once the
    // wildcard would usually happen to return the command first and a broken
    // implementation would pass. Here the only process ready to reap for the
    // first 200 ms is the orphan, so a wildcard cannot help but take it.
    const pid_t command = ::fork();
    ASSERT_GE(command, 0);
    if (command == 0) {
        ::usleep(200000);
        ::_exit(7);
    }

    const pid_t orphan = ::fork();
    ASSERT_GE(orphan, 0);
    if (orphan == 0) {
        ::_exit(42);
    }

    afrunner::Reaped total{};
    for (int attempt = 0; attempt < 2000; ++attempt) {
        const afrunner::Reaped got = afrunner::reap(command);
        total.orphans += got.orphans;
        if (got.child_reaped) {
            total.child_reaped = true;
            total.child_status = got.child_status;
        }
        if (total.child_reaped && total.orphans >= 1) {
            break;
        }
        ::usleep(1000);
    }

    EXPECT_TRUE(total.child_reaped);
    ASSERT_TRUE(WIFEXITED(total.child_status));
    EXPECT_EQ(WEXITSTATUS(total.child_status), 7)
        << "the orphan's 42 was attributed to the command";
    EXPECT_GE(total.orphans, 1);
}

TEST(Signals, ReapingWhenNothingIsLeftIsHarmless) {
    const afrunner::Reaped got = afrunner::reap(-1);
    EXPECT_EQ(got.orphans, 0);
    EXPECT_FALSE(got.child_reaped);
}

}  // namespace
