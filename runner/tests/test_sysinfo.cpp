#include "sysinfo.hpp"

#include <gtest/gtest.h>

#include <unistd.h>

#include <string>

#include "clock.hpp"

namespace {

TEST(SysInfo, ArchIsRecorded) {
    const std::string arch = afrunner::arch();
    EXPECT_FALSE(arch.empty());
    EXPECT_NE(arch, "unknown");
}

TEST(SysInfo, ShellIsAnAbsoluteResolvedPath) {
    const std::string shell = afrunner::shell_path();
    ASSERT_FALSE(shell.empty());
    EXPECT_EQ(shell.front(), '/');
}

TEST(SysInfo, Pid1MatchesTheRealProcessId) {
    EXPECT_EQ(afrunner::is_pid1(), ::getpid() == 1);
}

TEST(Clock, MonotonicNeverGoesBackwards) {
    const long long first = afrunner::monotonic_ms();
    const long long second = afrunner::monotonic_ms();
    EXPECT_GE(second, first);
}

TEST(Clock, RealtimeIsAPlausibleUnixTimestamp) {
    // 2020-01-01. A clock below this is unset, which would make every run
    // record sort before every other run.
    EXPECT_GT(afrunner::unix_ms(), 1577836800000LL);
}

// D12: a duration is never a difference of two realtime timestamps. The two
// clocks read from different origins, so they cannot be the same source.
TEST(Clock, TheTwoClocksAreDifferentSources) {
    EXPECT_NE(afrunner::unix_ms(), afrunner::monotonic_ms());
}

}  // namespace
