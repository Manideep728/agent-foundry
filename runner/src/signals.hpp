#pragma once

#include <sys/types.h>

#include <string>

namespace afrunner {

// Blocks SIGTERM, SIGINT and SIGCHLD so they are consumed through a signalfd
// in the poll loop instead of through a handler.
//
// A signal whose disposition is SIG_DFL is discarded for PID 1. A runner that
// installs nothing therefore ignores the SIGTERM from `docker stop`; Docker
// waits its full grace period, sends SIGKILL, and the container dies without
// writing a result at all.
//
// Must run before any fork, because the mask is inherited.
std::string block_signals();

// Becomes readable when one of the blocked signals arrives. -1 on failure.
int make_signalfd();

// Makes orphaned descendants reparent to this process instead of to the real
// PID 1. A no-op when the process already is PID 1, and unprivileged
// everywhere, so orphan reaping is exercised outside a container too.
void become_subreaper();

// Watches the signalfd for a request to stop.
//
// The whole abort path has to fit inside Docker's stop grace period, which is
// ten seconds by default: kill the group, mark the rest not_run, write a
// partial result, exit 7.
class AbortWatch {
  public:
    // -1 means nothing is watched and the run can never be aborted.
    explicit AbortWatch(int fd) : fd_(fd) {}

    int fd() const { return fd_; }

    // Drains the descriptor. Signals coalesce, so this counts the reads it
    // makes rather than trusting any per-signal delivery.
    void consume();

    bool requested() const { return stop_requests_ > 0; }

    // Asked twice. The operator means it, so the grace period is skipped.
    bool immediate() const { return stop_requests_ > 1; }

  private:
    int fd_;
    int stop_requests_ = 0;
};

struct Reaped {
    int orphans = 0;
    bool child_reaped = false;
    int child_status = 0;
};

// Collects the command, then every orphan that is ready.
//
// The command's status must come from waitpid(child, ...) and never from
// waitpid(-1, ...): as PID 1 the wildcard can return an unrelated orphan, and
// attributing its exit code and duration to the command produces a wrong
// measurement with nothing appearing to go wrong. The wildcard loop here still
// watches for the child, because it can exit between the two calls, and losing
// its status would be the same bug by a slower route.
Reaped reap(pid_t child);

// Collects orphans that are ready now, and waits up to budget_ms for more.
//
// A command's drain loop reaps only while that command runs, so an orphan that
// exits in the instant after the loop finished has no later pass inside it.
// Both callers close that gap: run_command sweeps briefly after each command,
// and run_all sweeps again at the end of the run, which is where the contract
// counts orphans collected at shutdown. Returns as soon as no child is left,
// when a stop is requested, or when the budget is spent. An orphan that
// outlives the budget is not waited for: in the container it dies with PID 1.
int reap_remaining_orphans(long long budget_ms, AbortWatch& watch);

}  // namespace afrunner
