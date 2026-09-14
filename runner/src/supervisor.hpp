#pragma once

#include <string>

#include "options.hpp"
#include "result.hpp"

namespace afrunner {

struct CommandRun {
    CommandResult result;
    // Non-empty when the runner itself cannot continue, such as a log file it
    // cannot create. The caller turns this into exit 5.
    std::string fatal;
};

// Runs one command to completion: spawn, drain both pipes, reap.
CommandRun run_command(const Options& options, int index, const std::string& command);

}  // namespace afrunner
