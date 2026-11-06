#include "cgroup.hpp"

#include <gtest/gtest.h>

#include <fstream>
#include <string>

#include "temp_dir.hpp"

namespace {

using afrunner::read_oom_kills;
using afrunner::serialise_oom_kills;
using afrunner::testing::TempDir;

std::string write_events(const TempDir& dir, const std::string& text) {
    const std::string path = dir.file("memory.events");
    std::ofstream(path) << text;
    return path;
}

// The layout the kernel writes, copied from a container on kernel 7.0.
constexpr const char* events_after_one_kill =
    "low 0\nhigh 0\nmax 23\noom 1\noom_kill 1\noom_group_kill 0\nsock_throttled 0\n";

TEST(Cgroup, ReadsTheOomKillCounter) {
    const TempDir dir;
    EXPECT_EQ(read_oom_kills(write_events(dir, events_after_one_kill)), 1);
}

TEST(Cgroup, ReadsZeroAsZeroNotAsMissing) {
    const TempDir dir;
    EXPECT_EQ(read_oom_kills(write_events(dir, "oom 0\noom_kill 0\n")), 0);
}

TEST(Cgroup, DoesNotMistakeTheGroupKillCounterForIt) {
    const TempDir dir;
    EXPECT_EQ(read_oom_kills(write_events(dir, "oom_group_kill 4\n")), std::nullopt);
}

TEST(Cgroup, AMissingFileIsUnknownNotZero) {
    const TempDir dir;
    EXPECT_EQ(read_oom_kills(dir.file("absent")), std::nullopt);
}

TEST(Cgroup, AMalformedCounterIsUnknown) {
    const TempDir dir;
    for (const char* text : {"oom_kill \n", "oom_kill x\n", "oom_kill 3x\n",
                             "oom_kill -1\n", "oom_kill 99999999999999999999\n"}) {
        SCOPED_TRACE(text);
        EXPECT_EQ(read_oom_kills(write_events(dir, text)), std::nullopt);
    }
}

TEST(Cgroup, SerialisesACountAndAnUnknown) {
    EXPECT_EQ(serialise_oom_kills(2), R"({"oom_kills":2})");
    EXPECT_EQ(serialise_oom_kills(std::nullopt), R"({"oom_kills":null})");
}

}  // namespace
