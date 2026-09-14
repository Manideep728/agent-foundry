#include "supervisor.hpp"

#include <gtest/gtest.h>

#include <fstream>
#include <sstream>
#include <string>

#include "options.hpp"
#include "outcome.hpp"
#include "resultfile.hpp"
#include "temp_dir.hpp"

namespace {

using afrunner::CommandRun;
using afrunner::Options;
using afrunner::Outcome;
using afrunner::testing::TempDir;

Options options_for(const TempDir& dir) {
    Options options;
    options.workdir = dir.path();
    options.log_dir = dir.file("logs");
    options.result = dir.file("result.json");
    options.commands = dir.file("commands.txt");
    options.timeout_ms = 60000;
    options.total_timeout_ms = 60000;
    options.grace_ms = 500;
    options.max_log_bytes = 8388608;
    options.tail_bytes = 4096;
    options.drain_after_exit_ms = 2000;
    return options;
}

std::string contents(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    std::ostringstream buffer;
    buffer << file.rdbuf();
    return buffer.str();
}

TEST(Supervisor, CapturesBothStreamsAndTheExitCode) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "echo out; echo err >&2; exit 3");

    EXPECT_EQ(run.fatal, "");
    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_EQ(run.result.verdict.exit_code, 3);
    EXPECT_EQ(run.result.out.tail, "out\n");
    EXPECT_EQ(run.result.err.tail, "err\n");
    EXPECT_EQ(run.result.out.bytes_total, 4);
    EXPECT_EQ(run.result.out.bytes_written, 4);
}

TEST(Supervisor, WritesTheLogFilesTheReportNames) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 7, "echo hello");

    EXPECT_EQ(run.result.out.path, "cmd-007.stdout.log");
    EXPECT_EQ(run.result.err.path, "cmd-007.stderr.log");
    EXPECT_EQ(contents(options.log_dir + "/" + run.result.out.path), "hello\n");
}

// A command that prints nothing still gets a file, so the report can tell
// "produced no output" from "never ran", which carries an empty path.
TEST(Supervisor, ASilentCommandStillGetsAnEmptyLog) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "true");

    EXPECT_EQ(run.result.out.bytes_total, 0);
    EXPECT_EQ(run.result.out.path, "cmd-000.stdout.log");
    EXPECT_EQ(contents(options.log_dir + "/cmd-000.stdout.log"), "");
}

// The deadlock test. A Linux pipe holds about 64 KiB, so reaping before
// draining wedges both processes until the deadline fires and reports a
// timeout the command never had.
TEST(Supervisor, OneMegabyteOfOutputIsNotATimeout) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(
        options, 0, "head -c 1048576 /dev/zero | tr '\\0' x");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_EQ(run.result.verdict.exit_code, 0);
    EXPECT_FALSE(run.result.verdict.deadline_signal_sent);
    EXPECT_EQ(run.result.out.bytes_total, 1048576);
    EXPECT_EQ(run.result.out.bytes_written, 1048576);
    EXPECT_FALSE(run.result.out.bytes_written < run.result.out.bytes_total);
}

// Draining only one descriptor per wakeup passes the single-stream test and
// deadlocks here, because the unread pipe fills and blocks the writer.
TEST(Supervisor, BothStreamsFillAtOnce) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(
        options, 0,
        "head -c 1048576 /dev/zero | tr '\\0' o & "
        "head -c 1048576 /dev/zero | tr '\\0' e >&2; wait");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_EQ(run.result.out.bytes_total, 1048576);
    EXPECT_EQ(run.result.err.bytes_total, 1048576);
}

// Stopping the read at the cap blocks the child on a full pipe. The runner
// keeps reading and throws the excess away instead.
TEST(Supervisor, TruncationKeepsReadingPastTheCap) {
    const TempDir dir;
    Options options = options_for(dir);
    options.max_log_bytes = 4096;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(
        options, 0, "head -c 1048576 /dev/zero | tr '\\0' x; echo; echo THEEND");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_EQ(run.result.verdict.exit_code, 0);
    EXPECT_EQ(run.result.out.bytes_written, 4096);
    EXPECT_GT(run.result.out.bytes_total, 1048576);
    EXPECT_EQ(contents(options.log_dir + "/cmd-000.stdout.log").size(), 4096U);

    // The file holds the front, and the tail still holds the end.
    EXPECT_NE(run.result.out.tail.find("THEEND"), std::string::npos);
}

TEST(Supervisor, ReportsASpawnFailure) {
    const TempDir dir;
    Options options = options_for(dir);
    options.workdir = dir.file("gone");
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "true");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::SpawnFailed);
    EXPECT_NE(run.result.spawn_error, "");
    EXPECT_EQ(run.result.out.path, "") << "a command that never ran has no log";
}

TEST(Supervisor, ReportsACommandKilledByASignal) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "kill -TERM $$");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Signaled);
    EXPECT_EQ(run.result.verdict.signal, 15);
    EXPECT_FALSE(run.result.verdict.deadline_signal_sent);
}

}  // namespace
