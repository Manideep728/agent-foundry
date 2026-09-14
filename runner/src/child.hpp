#pragma once

#include <sys/types.h>

#include <string>

namespace afrunner {

struct Spawned {
    // The direct child, which is the shell. -1 when the spawn failed.
    pid_t pid = -1;
    // The child's process group, which is its own. Signals go to the group,
    // never to the bare process, or a backgrounded grandchild survives.
    pid_t pgid = -1;
    // The parent's read ends, both non-blocking. -1 when the spawn failed.
    int stdout_fd = -1;
    int stderr_fd = -1;
    // Non-empty when the spawn failed, in which case nothing else is set.
    std::string error;
};

// Starts `/bin/sh -c command` with the working directory set to `workdir`.
//
// The caller owns stdout_fd and stderr_fd and must close them.
Spawned spawn(const std::string& command, const std::string& workdir);

}  // namespace afrunner
