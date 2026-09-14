#pragma once

#include <string>
#include <vector>

#include "options.hpp"
#include "result.hpp"
#include "signals.hpp"

namespace afrunner {

struct RunAll {
    Report report;
    // Non-empty when the runner itself cannot continue. Becomes exit 5.
    std::string fatal;
};

// Runs every command in order and builds the whole report.
//
// The overload without a watch can never be aborted, which is what the unit
// tests want.
RunAll run_all(const Options& options, const std::vector<std::string>& commands);
RunAll run_all(const Options& options, const std::vector<std::string>& commands,
               AbortWatch& watch);

struct CommandRun {
    CommandResult result;
    // Non-empty when the runner itself cannot continue, such as a log file it
    // cannot create. The caller turns this into exit 5.
    std::string fatal;
};

// Runs one command to completion: spawn, drain both pipes, reap.
CommandRun run_command(const Options& options, int index, const std::string& command);
CommandRun run_command(const Options& options, int index, const std::string& command,
                       AbortWatch& watch);

}  // namespace afrunner
