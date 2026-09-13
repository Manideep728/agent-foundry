#include "outcome.hpp"

#include <sys/wait.h>

#include <csignal>

namespace afrunner {

const char* to_string(Outcome outcome) {
    switch (outcome) {
        case Outcome::Exited:
            return "exited";
        case Outcome::Signaled:
            return "signaled";
        case Outcome::Timeout:
            return "timeout";
        case Outcome::SpawnFailed:
            return "spawn_failed";
        case Outcome::NotRun:
            break;
    }
    return "not_run";
}

Verdict not_run() { return Verdict{}; }

Verdict decide_outcome(const Facts& facts) {
    Verdict verdict;

    if (!facts.spawned) {
        verdict.outcome = Outcome::SpawnFailed;
        return verdict;
    }

    // "The deadline fired, whatever happened next" -- including the child
    // beating the signal. This is what stops the scorer counting a command
    // that blew its budget as a pass.
    verdict.deadline_signal_sent = facts.deadline_fired;

    // Not `escalated` alone. CommandResult.__post_init__ refuses
    // killed_after_grace without deadline_signal_sent, so an abort that
    // escalates to SIGKILL must leave this false or the control plane cannot
    // parse the result at all.
    verdict.killed_after_grace = facts.escalated && facts.deadline_fired;

    if (!facts.reaped) {
        // SIGKILL did not clear the group, which happens when a process is in
        // uninterruptible sleep. The signal number is the last thing we sent
        // rather than something observed, and group_escaped says so.
        verdict.group_escaped = true;
        verdict.signal = SIGKILL;
        verdict.outcome = facts.deadline_fired ? Outcome::Timeout : Outcome::Signaled;
        return verdict;
    }

    if (WIFEXITED(facts.status)) {
        verdict.outcome = Outcome::Exited;
        verdict.exit_code = WEXITSTATUS(facts.status);
        return verdict;
    }

    // A reaped child that did not exit was terminated by a signal. waitpid is
    // called without WUNTRACED, so a stop status never arrives here.
    verdict.signal = WTERMSIG(facts.status);

    // The question is whether what we sent killed it, not whether we had to
    // escalate. `sleep` obeys SIGTERM and dies inside the grace window, so it
    // is a timeout that was never escalated.
    const bool died_from_our_signal = facts.signal_sent;
    verdict.outcome = (facts.deadline_fired && died_from_our_signal) ? Outcome::Timeout
                                                                    : Outcome::Signaled;
    return verdict;
}

}  // namespace afrunner
