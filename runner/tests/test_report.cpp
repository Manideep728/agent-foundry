#include "report.hpp"

#include <gtest/gtest.h>

#include <csignal>
#include <string>

namespace {

using afrunner::CommandResult;
using afrunner::ExitReason;
using afrunner::Outcome;
using afrunner::Report;

constexpr long long epoch_ms = 1700000000000;

Report minimal_report() {
    CommandResult command;
    command.index = 0;
    command.command = "true";
    command.started_at_unix_ms = epoch_ms;
    command.duration_ms = 5;
    command.verdict.outcome = Outcome::Exited;
    command.verdict.exit_code = 0;
    command.out.path = "cmd-000.stdout.log";
    command.err.path = "cmd-000.stderr.log";

    Report report;
    report.runner_version = "afrunner 0.1.0";
    report.arch = "x86_64";
    report.shell = "/bin/dash";
    report.workdir = "/work";
    report.started_at_unix_ms = epoch_ms;
    report.wall_ms = 12;
    report.commands.push_back(command);
    return report;
}

TEST(Report, SerialisesTheWholeDocument) {
    const std::string expected =
        R"({"schema_version":1,"runner_version":"afrunner 0.1.0","arch":"x86_64",)"
        R"("pid1":false,"shell":"/bin/dash","workdir":"/work",)"
        R"("started_at_unix_ms":1700000000000,"wall_ms":12,"exit_reason":"completed",)"
        R"("aborted":false,"orphans_reaped_total":0,"commands":[)"
        R"({"index":0,"command":"true","started_at_unix_ms":1700000000000,)"
        R"("duration_ms":5,"outcome":"exited","exit_code":0,"signal":null,)"
        R"("timed_out":false,"deadline_signal_sent":false,"killed_after_grace":false,)"
        R"("group_escaped":false,"drain_deadline_hit":false,"orphans_reaped":0,)"
        R"("spawn_error":null,)"
        R"("stdout":{"bytes_total":0,"bytes_written":0,"truncated":false,)"
        R"("path":"cmd-000.stdout.log","tail":""},)"
        R"("stderr":{"bytes_total":0,"bytes_written":0,"truncated":false,)"
        R"("path":"cmd-000.stderr.log","tail":""}}]})";

    EXPECT_EQ(afrunner::serialise(minimal_report()), expected);
}

TEST(Report, TruncatedFollowsTheByteCounts) {
    Report report = minimal_report();
    report.commands[0].out.bytes_total = 1000;
    report.commands[0].out.bytes_written = 400;

    const std::string text = afrunner::serialise(report);
    EXPECT_NE(text.find(R"("bytes_total":1000,"bytes_written":400,"truncated":true)"),
              std::string::npos);
}

TEST(Report, AbortedFollowsTheExitReason) {
    Report report = minimal_report();
    report.exit_reason = ExitReason::Signal;

    const std::string text = afrunner::serialise(report);
    EXPECT_NE(text.find(R"("exit_reason":"signal","aborted":true)"), std::string::npos);
}

TEST(Report, TimedOutFollowsTheOutcome) {
    Report report = minimal_report();
    report.commands[0].verdict.outcome = Outcome::Timeout;
    report.commands[0].verdict.exit_code.reset();
    report.commands[0].verdict.signal = SIGKILL;
    report.commands[0].verdict.deadline_signal_sent = true;

    const std::string text = afrunner::serialise(report);
    EXPECT_NE(text.find(R"("outcome":"timeout")"), std::string::npos);
    EXPECT_NE(text.find(R"("timed_out":true)"), std::string::npos);
    EXPECT_NE(text.find(R"("exit_code":null)"), std::string::npos);
}

TEST(Report, SpawnFailureCarriesItsMessage) {
    Report report = minimal_report();
    report.commands[0].verdict.outcome = Outcome::SpawnFailed;
    report.commands[0].verdict.exit_code.reset();
    report.commands[0].spawn_error = "fork: Resource temporarily unavailable";

    EXPECT_EQ(afrunner::check_invariants(report), "");
    const std::string text = afrunner::serialise(report);
    EXPECT_NE(text.find(R"("spawn_error":"fork: Resource temporarily unavailable")"),
              std::string::npos);
}

TEST(Report, AWellFormedReportHasNoViolation) {
    EXPECT_EQ(afrunner::check_invariants(minimal_report()), "");
}

TEST(Report, RejectsABlankArch) {
    Report report = minimal_report();
    report.arch = "   ";
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsAReportWithNoCommands) {
    Report report = minimal_report();
    report.commands.clear();
    EXPECT_NE(afrunner::check_invariants(report), "");
}

// The reader requires indices to be exactly 0..n-1, so a partial result must
// still carry an entry for every command in the file.
TEST(Report, RejectsNonContiguousIndices) {
    Report report = minimal_report();
    CommandResult second = report.commands[0];
    second.index = 2;
    report.commands.push_back(second);

    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsAnExitCodeWithoutAnExit) {
    Report report = minimal_report();
    report.commands[0].verdict.outcome = Outcome::Signaled;
    report.commands[0].verdict.signal = SIGTERM;
    // exit_code left set.
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsASignalWithoutASignalledOutcome) {
    Report report = minimal_report();
    report.commands[0].verdict.signal = SIGTERM;
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsASpawnErrorOnACommandThatRan) {
    Report report = minimal_report();
    report.commands[0].spawn_error = "should not be here";
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsASpawnFailureWithNoMessage) {
    Report report = minimal_report();
    report.commands[0].verdict.outcome = Outcome::SpawnFailed;
    report.commands[0].verdict.exit_code.reset();
    EXPECT_NE(afrunner::check_invariants(report), "");
}

// The landmine from the abort path: escalating to SIGKILL without a deadline
// must not set this, or the control plane refuses the whole result.
TEST(Report, RejectsKilledAfterGraceWithoutTheDeadline) {
    Report report = minimal_report();
    report.commands[0].verdict.killed_after_grace = true;
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsMoreBytesWrittenThanProduced) {
    Report report = minimal_report();
    report.commands[0].out.bytes_total = 10;
    report.commands[0].out.bytes_written = 11;
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, RejectsANegativeDuration) {
    Report report = minimal_report();
    report.commands[0].duration_ms = -1;
    EXPECT_NE(afrunner::check_invariants(report), "");
}

TEST(Report, NamesTheExitReasonsTheContractUses) {
    EXPECT_STREQ(to_string(ExitReason::Completed), "completed");
    EXPECT_STREQ(to_string(ExitReason::TotalTimeout), "total_timeout");
    EXPECT_STREQ(to_string(ExitReason::Signal), "signal");
}

}  // namespace
