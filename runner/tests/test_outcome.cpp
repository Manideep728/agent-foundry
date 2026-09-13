#include "outcome.hpp"

#include <gtest/gtest.h>

#include <csignal>
#include <string>

namespace {

using afrunner::decide_outcome;
using afrunner::Facts;
using afrunner::Outcome;
using afrunner::timed_out;
using afrunner::Verdict;

// Linux packs a wait status as (exit_code << 8) for a normal exit, and as the
// bare signal number for a termination.
constexpr int exited_status(int code) { return (code & 0xFF) << 8; }
constexpr int signaled_status(int signal) { return signal & 0x7F; }

// Row 1.
TEST(Outcome, ASpawnFailureNeedsNoOtherFact) {
    const Verdict verdict = decide_outcome(Facts{.spawned = false});

    EXPECT_EQ(verdict.outcome, Outcome::SpawnFailed);
    EXPECT_FALSE(verdict.exit_code.has_value());
    EXPECT_FALSE(verdict.signal.has_value());
    EXPECT_FALSE(verdict.deadline_signal_sent);
    EXPECT_FALSE(verdict.killed_after_grace);
    EXPECT_FALSE(verdict.group_escaped);
}

// Row 2.
TEST(Outcome, AnUndisturbedExitIsJustAnExit) {
    const Verdict verdict = decide_outcome(
        Facts{.spawned = true, .reaped = true, .status = exited_status(0)});

    EXPECT_EQ(verdict.outcome, Outcome::Exited);
    EXPECT_EQ(verdict.exit_code, 0);
    EXPECT_FALSE(verdict.signal.has_value());
    EXPECT_FALSE(verdict.deadline_signal_sent);
}

TEST(Outcome, ANonZeroExitIsStillAnExit) {
    const Verdict verdict = decide_outcome(
        Facts{.spawned = true, .reaped = true, .status = exited_status(1)});

    EXPECT_EQ(verdict.outcome, Outcome::Exited);
    EXPECT_EQ(verdict.exit_code, 1);
}

// Row 3: a signal nobody in this program sent.
TEST(Outcome, AnUnprovokedSignalIsACrash) {
    const Verdict verdict = decide_outcome(
        Facts{.spawned = true, .reaped = true, .status = signaled_status(SIGSEGV)});

    EXPECT_EQ(verdict.outcome, Outcome::Signaled);
    EXPECT_EQ(verdict.signal, SIGSEGV);
    EXPECT_FALSE(verdict.exit_code.has_value());
    EXPECT_FALSE(verdict.deadline_signal_sent);
}

// Row 4. The command beat the signal by microseconds. The wait status is the
// authority for reporting, so this is an honest exit; deadline_signal_sent is
// what stops the control plane scoring it as a pass.
TEST(Outcome, ACleanExitAfterTheDeadlineStaysAnHonestExit) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = true,
                                                 .status = exited_status(0),
                                                 .deadline_fired = true,
                                                 .signal_sent = true});

    EXPECT_EQ(verdict.outcome, Outcome::Exited);
    EXPECT_EQ(verdict.exit_code, 0);
    EXPECT_FALSE(timed_out(verdict));
    EXPECT_TRUE(verdict.deadline_signal_sent);
}

// Row 5, no escalation. This is BUGS.md bug 1 exactly: `sleep 300 & wait`
// obeys SIGTERM and dies inside the grace window, so nothing was escalated --
// and it is still a timeout.
TEST(Outcome, ACommandThatObeysSigtermIsATimeoutWithoutEscalation) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = true,
                                                 .status = signaled_status(SIGTERM),
                                                 .deadline_fired = true,
                                                 .signal_sent = true,
                                                 .escalated = false});

    EXPECT_EQ(verdict.outcome, Outcome::Timeout);
    EXPECT_TRUE(timed_out(verdict));
    EXPECT_EQ(verdict.signal, SIGTERM);
    EXPECT_TRUE(verdict.deadline_signal_sent);
    EXPECT_FALSE(verdict.killed_after_grace);
}

// Row 5, escalated: `trap "" TERM; sleep 300`. Same outcome as above, and the
// only difference is the column that answers a different question.
TEST(Outcome, ACommandThatIgnoresSigtermIsATimeoutWithEscalation) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = true,
                                                 .status = signaled_status(SIGKILL),
                                                 .deadline_fired = true,
                                                 .signal_sent = true,
                                                 .escalated = true});

    EXPECT_EQ(verdict.outcome, Outcome::Timeout);
    EXPECT_EQ(verdict.signal, SIGKILL);
    EXPECT_TRUE(verdict.deadline_signal_sent);
    EXPECT_TRUE(verdict.killed_after_grace);
}

// Row 6: the runner was aborted, so no deadline fired.
TEST(Outcome, AnExitDuringAnAbortIsNotATimeout) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = true,
                                                 .status = exited_status(3),
                                                 .deadline_fired = false,
                                                 .signal_sent = true});

    EXPECT_EQ(verdict.outcome, Outcome::Exited);
    EXPECT_EQ(verdict.exit_code, 3);
    EXPECT_FALSE(verdict.deadline_signal_sent);
}

// Row 7.
TEST(Outcome, ASignalDuringAnAbortIsSignaledNotTimeout) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = true,
                                                 .status = signaled_status(SIGTERM),
                                                 .deadline_fired = false,
                                                 .signal_sent = true});

    EXPECT_EQ(verdict.outcome, Outcome::Signaled);
    EXPECT_EQ(verdict.signal, SIGTERM);
    EXPECT_FALSE(verdict.deadline_signal_sent);
}

// Row 8: SIGKILL did not clear the group.
TEST(Outcome, AnEscapedGroupAfterTheDeadlineIsATimeout) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = false,
                                                 .deadline_fired = true,
                                                 .signal_sent = true,
                                                 .escalated = true});

    EXPECT_EQ(verdict.outcome, Outcome::Timeout);
    EXPECT_TRUE(verdict.group_escaped);
    EXPECT_EQ(verdict.signal, SIGKILL) << "inferred, not observed";
    EXPECT_TRUE(verdict.deadline_signal_sent);
    EXPECT_TRUE(verdict.killed_after_grace);
}

// Row 9. The escalation happened, but no deadline did, so killed_after_grace
// must stay false or the report becomes unparseable.
TEST(Outcome, AnEscapedGroupDuringAnAbortIsSignaled) {
    const Verdict verdict = decide_outcome(Facts{.spawned = true,
                                                 .reaped = false,
                                                 .deadline_fired = false,
                                                 .signal_sent = true,
                                                 .escalated = true});

    EXPECT_EQ(verdict.outcome, Outcome::Signaled);
    EXPECT_TRUE(verdict.group_escaped);
    EXPECT_FALSE(verdict.deadline_signal_sent);
    EXPECT_FALSE(verdict.killed_after_grace);
}

TEST(Outcome, NotRunCarriesNothing) {
    const Verdict verdict = afrunner::not_run();

    EXPECT_EQ(verdict.outcome, Outcome::NotRun);
    EXPECT_FALSE(verdict.exit_code.has_value());
    EXPECT_FALSE(verdict.signal.has_value());
    EXPECT_FALSE(verdict.deadline_signal_sent);
    EXPECT_FALSE(verdict.killed_after_grace);
    EXPECT_FALSE(verdict.group_escaped);
}

TEST(Outcome, NamesMatchTheContractVocabulary) {
    EXPECT_STREQ(to_string(Outcome::Exited), "exited");
    EXPECT_STREQ(to_string(Outcome::Signaled), "signaled");
    EXPECT_STREQ(to_string(Outcome::Timeout), "timeout");
    EXPECT_STREQ(to_string(Outcome::SpawnFailed), "spawn_failed");
    EXPECT_STREQ(to_string(Outcome::NotRun), "not_run");
}

// Every rule CommandResult.__post_init__ enforces, checked over every
// combination of facts. A verdict that fails one of these is a result.json the
// control plane refuses outright, which surfaces weeks later as a missing
// measurement rather than as a bug here.
TEST(Outcome, EveryFactCombinationSatisfiesTheReaderInvariants) {
    const int statuses[] = {exited_status(0), exited_status(1),
                            signaled_status(SIGTERM), signaled_status(SIGKILL)};

    for (const bool spawned : {false, true}) {
        for (const bool reaped : {false, true}) {
            for (const bool deadline_fired : {false, true}) {
                for (const bool signal_sent : {false, true}) {
                    for (const bool escalated : {false, true}) {
                        for (const int status : statuses) {
                            const Facts facts{.spawned = spawned,
                                              .reaped = reaped,
                                              .status = status,
                                              .deadline_fired = deadline_fired,
                                              .signal_sent = signal_sent,
                                              .escalated = escalated};
                            const Verdict verdict = decide_outcome(facts);

                            SCOPED_TRACE("spawned=" + std::to_string(spawned) +
                                         " reaped=" + std::to_string(reaped) +
                                         " deadline=" + std::to_string(deadline_fired) +
                                         " signalled=" + std::to_string(signal_sent) +
                                         " escalated=" + std::to_string(escalated) +
                                         " status=" + std::to_string(status));

                            EXPECT_EQ(verdict.exit_code.has_value(),
                                      verdict.outcome == Outcome::Exited);
                            EXPECT_EQ(verdict.signal.has_value(),
                                      verdict.outcome == Outcome::Signaled ||
                                          verdict.outcome == Outcome::Timeout);
                            EXPECT_EQ(timed_out(verdict),
                                      verdict.outcome == Outcome::Timeout);
                            if (verdict.outcome == Outcome::Timeout) {
                                EXPECT_TRUE(verdict.deadline_signal_sent);
                            }
                            if (verdict.killed_after_grace) {
                                EXPECT_TRUE(verdict.deadline_signal_sent);
                            }
                        }
                    }
                }
            }
        }
    }
}

}  // namespace
