#include "supervisor.hpp"

#include <gtest/gtest.h>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <fstream>
#include <sstream>
#include <string>

#include "options.hpp"
#include "outcome.hpp"
#include "report.hpp"
#include "resultfile.hpp"
#include "signals.hpp"
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

// Two bugs, two assertions. The exit codes are deliberately different so a
// waitpid(-1) misattribution reads as 42 instead of 7, and orphans_reaped
// catches the opposite failure of never reaping at all.
TEST(Supervisor, ADoubleForkedOrphanIsReapedAndNotAttributed) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run =
        afrunner::run_command(options, 0, "( sleep 0.2; exit 42 ) & exit 7");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_EQ(run.result.verdict.exit_code, 7) << "the orphan's 42 leaked in";
    EXPECT_GE(run.result.orphans_reaped, 1);
}

// The per-command half of the orphan sweep, made certain rather than raced.
// The orphan redirects its output, so it does not hold the command's pipes:
// the drain loop ends as soon as the shell exits, and the orphan becomes
// reapable afterwards. That is exactly the gap that used to leave a zombie.
// The orphan exits well inside the post-command budget, so this command reaps
// it rather than leaving it to the sweep at the end of the run.
TEST(Supervisor, ReapsAnOrphanThatExitsJustAfterTheDrainLoop) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run =
        afrunner::run_command(options, 0, "sleep 0.002 >/dev/null 2>&1 & exit 0");

    EXPECT_EQ(run.result.verdict.exit_code, 0);
    EXPECT_EQ(run.result.orphans_reaped, 1);

    int leftover = 0;
    EXPECT_EQ(::waitpid(-1, &leftover, WNOHANG), -1) << "a zombie was left behind";
    EXPECT_EQ(errno, ECHILD);
}

// Unreaped orphans accumulate as zombies, consume PID slots against
// --pids-limit, and make later commands fail to spawn.
TEST(Supervisor, ManyOrphansLeaveNoZombies) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    std::string command;
    for (int i = 0; i < 20; ++i) {
        command += "( sleep 0.15 ) & ";
    }
    command += "exit 0";

    const CommandRun run = afrunner::run_command(options, 0, command);

    EXPECT_EQ(run.result.verdict.exit_code, 0);
    EXPECT_GE(run.result.orphans_reaped, 20);

    int leftover = 0;
    EXPECT_EQ(::waitpid(-1, &leftover, WNOHANG), -1) << "a zombie was left behind";
    EXPECT_EQ(errno, ECHILD);
}

// BUGS.md bug 1, end to end. `sleep` obeys SIGTERM and dies inside the grace
// window, so nothing is escalated -- and it is still a timeout. Recording it
// as a spontaneous crash would put the benchmark's timeout rate near zero
// while looking entirely plausible.
//
// The command is compound on purpose. dash optimises `sh -c '<simple
// command>'` into a bare exec, so `sh -c 'sleep 300'` creates no grandchild
// and the test would pass whether or not group cleanup works.
TEST(Supervisor, ACommandThatObeysSigtermIsStillATimeout) {
    const TempDir dir;
    Options options = options_for(dir);
    options.timeout_ms = 300;
    options.grace_ms = 5000;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "sleep 300 & wait");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Timeout);
    EXPECT_TRUE(run.result.verdict.deadline_signal_sent);
    EXPECT_FALSE(run.result.verdict.killed_after_grace)
        << "it obeyed SIGTERM, so nothing was escalated";
    EXPECT_LT(run.result.duration_ms, 4000)
        << "it should die well inside the 5s grace window";
}

TEST(Supervisor, ACommandThatIgnoresSigtermIsKilledAfterTheGrace) {
    const TempDir dir;
    Options options = options_for(dir);
    options.timeout_ms = 300;
    options.grace_ms = 500;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run =
        afrunner::run_command(options, 0, "trap '' TERM; sleep 300 & wait");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Timeout);
    EXPECT_TRUE(run.result.verdict.deadline_signal_sent);
    EXPECT_TRUE(run.result.verdict.killed_after_grace);
    EXPECT_EQ(run.result.verdict.signal, SIGKILL);
}

// The guard against D5's caveat quietly invalidating the group tests: if the
// shell ever stops forking a layer, this fails loudly instead of the others
// passing for the wrong reason.
TEST(Supervisor, TheShellReallyForksALayer) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run =
        afrunner::run_command(options, 0, "sleep 0.05 & echo $!; wait");

    const long long grandchild = std::stoll(run.result.out.tail);
    EXPECT_GT(grandchild, 0);
}

// Killing the bare process leaves a backgrounded grandchild running, holding
// the pipe open and a PID slot with it. Signalling the group is what cleans up.
TEST(Supervisor, ABackgroundedGrandchildDoesNotHoldTheRunOpen) {
    const TempDir dir;
    Options options = options_for(dir);
    options.timeout_ms = 300;
    options.grace_ms = 500;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run =
        afrunner::run_command(options, 0, "sleep 300 & echo $!; wait");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Timeout);
    EXPECT_LT(run.result.duration_ms, 20000) << "the run must not wait out the sleep";

    const long long grandchild = std::stoll(run.result.out.tail);
    ASSERT_GT(grandchild, 0);
    // Without this the test only proves the runner did not hang, not that it
    // cleaned up after itself.
    ::usleep(200000);
    EXPECT_EQ(::kill(static_cast<pid_t>(grandchild), 0), -1);
    EXPECT_EQ(errno, ESRCH) << "the backgrounded grandchild survived the run";
}

TEST(Supervisor, ACommandFinishingInTimeIsNotATimeout) {
    const TempDir dir;
    Options options = options_for(dir);
    options.timeout_ms = 5000;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const CommandRun run = afrunner::run_command(options, 0, "sleep 0.05 & wait");

    EXPECT_EQ(run.result.verdict.outcome, Outcome::Exited);
    EXPECT_FALSE(run.result.verdict.deadline_signal_sent);
    EXPECT_FALSE(run.result.verdict.killed_after_grace);
}

TEST(RunAll, RunsEveryCommandInOrder) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all =
        afrunner::run_all(options, {"echo one", "echo two", "exit 5"});

    ASSERT_EQ(all.report.commands.size(), 3U);
    EXPECT_EQ(all.report.exit_reason, afrunner::ExitReason::Completed);
    EXPECT_EQ(all.report.commands[0].out.tail, "one\n");
    EXPECT_EQ(all.report.commands[1].out.tail, "two\n");
    EXPECT_EQ(all.report.commands[2].verdict.exit_code, 5);
    EXPECT_EQ(afrunner::check_invariants(all.report), "");
}

// BUGS.md's second lesson from Phase 3: leaving slack in the total budget let
// the next command fit inside it, so the assertion ran against a run that had
// legitimately completed. total == per-command guarantees nothing fits after.
TEST(RunAll, ExhaustingTheTotalBudgetLeavesLaterCommandsNotRun) {
    const TempDir dir;
    Options options = options_for(dir);
    options.timeout_ms = 300;
    options.total_timeout_ms = 300;
    options.grace_ms = 200;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all =
        afrunner::run_all(options, {"sleep 300 & wait", "echo never"});

    ASSERT_EQ(all.report.commands.size(), 2U);
    EXPECT_EQ(all.report.commands[0].verdict.outcome, Outcome::Timeout);
    EXPECT_EQ(all.report.commands[1].verdict.outcome, Outcome::NotRun);
    EXPECT_EQ(all.report.exit_reason, afrunner::ExitReason::TotalTimeout);
    EXPECT_EQ(afrunner::check_invariants(all.report), "");
}

TEST(RunAll, StopOnFailureSkipsTheRest) {
    const TempDir dir;
    Options options = options_for(dir);
    options.stop_on_failure = true;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all =
        afrunner::run_all(options, {"true", "exit 1", "echo never"});

    EXPECT_EQ(all.report.commands[0].verdict.exit_code, 0);
    EXPECT_EQ(all.report.commands[1].verdict.exit_code, 1);
    EXPECT_EQ(all.report.commands[2].verdict.outcome, Outcome::NotRun);
    // A truncation the caller asked for is not the runner being cut short.
    EXPECT_EQ(all.report.exit_reason, afrunner::ExitReason::Completed);
}

TEST(RunAll, WithoutStopOnFailureAFailureDoesNotStopTheRun) {
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all = afrunner::run_all(options, {"exit 1", "echo after"});

    EXPECT_EQ(all.report.commands[1].out.tail, "after\n");
}

TEST(RunAll, CountsOrphansAcrossEveryCommand) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all = afrunner::run_all(
        options, {"( sleep 0.1 ) & exit 0", "( sleep 0.1 ) & exit 0"});

    EXPECT_GE(all.report.orphans_reaped_total, 2);
}

// The regression behind a CI failure: the last command's orphan exited just
// after that command's loop ended, and nothing reaped it. Redirecting the
// orphan's output makes the gap certain rather than a race, because the orphan
// no longer holds the pipe that keeps the command's loop running.
TEST(RunAll, CountsAnOrphanThatExitsAfterTheLastCommand) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");

    const afrunner::RunAll all =
        afrunner::run_all(options, {"sleep 0.2 >/dev/null 2>&1 & exit 0"});

    EXPECT_EQ(all.report.commands[0].orphans_reaped, 0);
    EXPECT_EQ(all.report.orphans_reaped_total, 1);
}

TEST(RunAll, AnOrphanThatOutlivesTheBudgetDoesNotHoldTheRun) {
    afrunner::become_subreaper();
    const TempDir dir;
    Options options = options_for(dir);
    options.drain_after_exit_ms = 200;
    ASSERT_EQ(afrunner::ensure_directory(options.log_dir), "");
    const std::string pid_file = dir.file("orphan.pid");

    const afrunner::RunAll all = afrunner::run_all(
        options, {"sleep 30 >/dev/null 2>&1 & echo $! > " + pid_file + "; exit 0"});

    EXPECT_LT(all.report.wall_ms, 5000);
    EXPECT_EQ(all.report.orphans_reaped_total, 0);

    // The orphan is this test process's child now; kill and reap it so it
    // cannot be counted by a later test in the same process.
    std::ifstream pid_stream(pid_file);
    pid_t orphan = 0;
    ASSERT_TRUE(pid_stream >> orphan);
    ::kill(orphan, SIGKILL);
    int status = 0;
    ::waitpid(orphan, &status, 0);
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
