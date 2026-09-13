#pragma once

#include <string>
#include <vector>

#include "outcome.hpp"

namespace afrunner {

enum class ExitReason {
    Completed,
    TotalTimeout,
    Signal,
};

const char* to_string(ExitReason reason);

struct StreamCapture {
    // What the process produced.
    long long bytes_total = 0;
    // What reached disk. The runner keeps reading past --max-log-bytes and
    // discards the excess, so this can be the smaller of the two.
    long long bytes_written = 0;
    // A bare basename inside --log-dir, such as cmd-000.stdout.log. Empty when
    // the command never ran, which is how "produced no output" stays
    // distinguishable from "never started".
    std::string path;
    std::string tail;

    // `truncated` is derived at serialisation, never stored.
};

struct CommandResult {
    int index = 0;
    std::string command;
    long long started_at_unix_ms = 0;
    long long duration_ms = 0;
    Verdict verdict;
    // Set only when the outcome is spawn_failed.
    std::string spawn_error;
    long long orphans_reaped = 0;
    // Output was still arriving after the child was reaped. Observed by the
    // drain loop rather than decided, so it lives here and not in Verdict.
    bool drain_deadline_hit = false;
    StreamCapture out;
    StreamCapture err;
};

struct Report {
    std::string runner_version;
    std::string arch;
    bool pid1 = false;
    std::string shell;
    std::string workdir;
    // Realtime. Orders runs, and is never subtracted.
    long long started_at_unix_ms = 0;
    // Monotonic. Measures duration, and is never a difference of timestamps.
    long long wall_ms = 0;
    ExitReason exit_reason = ExitReason::Completed;
    long long orphans_reaped_total = 0;
    std::vector<CommandResult> commands;

    // `aborted` is derived at serialisation, never stored.
};

}  // namespace afrunner
