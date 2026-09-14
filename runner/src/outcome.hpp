#pragma once

#include <optional>

namespace afrunner {

enum class Outcome {
    Exited,
    Signaled,
    Timeout,
    SpawnFailed,
    NotRun,
};

const char* to_string(Outcome outcome);

// The facts a command's fate is decided from, kept apart on purpose.
//
// BUGS.md bug 1 was one boolean answering two questions: "did the command die
// because of a signal we sent", which makes it a timeout, and "did we have to
// escalate past SIGTERM", which is killed_after_grace. Collapsing them
// recorded every well-behaved program that blew its deadline as a crash, and
// no scoring test could see it.
struct Facts {
    // fork and exec both succeeded.
    bool spawned = false;
    // waitpid(child_pid, ...) returned a status. False means the group
    // outlived SIGKILL.
    bool reaped = false;
    // That wait status. Meaningless unless `reaped`.
    int status = 0;
    // The command's deadline passed. A clock fact, nothing more.
    bool deadline_fired = false;
    // We signalled the group while the child was still alive.
    bool signal_sent = false;
    // We escalated to SIGKILL after the grace period.
    bool escalated = false;
};

struct Verdict {
    Outcome outcome = Outcome::NotRun;
    std::optional<int> exit_code;
    std::optional<int> signal;
    bool deadline_signal_sent = false;
    bool killed_after_grace = false;
    bool group_escaped = false;
};

Verdict decide_outcome(const Facts& facts);

// A command the run never reached.
Verdict not_run();

// Never stored. Keeping it derived is why it cannot contradict the outcome.
inline bool timed_out(const Verdict& verdict) {
    return verdict.outcome == Outcome::Timeout;
}

// Whether this command is evidence of success, which is not simply exit 0. A
// command that exits 0 after its deadline fired blew its budget, and counting
// it as a pass would inflate every success rate in the benchmark. Mirrors
// CommandResult.succeeded on the Python side, and defines what
// --stop-on-failure stops on.
inline bool succeeded(const Verdict& verdict) {
    return verdict.outcome == Outcome::Exited && verdict.exit_code == 0 &&
           !verdict.deadline_signal_sent;
}

}  // namespace afrunner
