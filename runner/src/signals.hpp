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

}  // namespace afrunner
