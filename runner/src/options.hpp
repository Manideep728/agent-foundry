#pragma once

#include <string>
#include <variant>

namespace afrunner {

struct Options {
    std::string commands;
    std::string workdir;
    std::string result;
    std::string log_dir;
    long long timeout_ms = 0;
    long long total_timeout_ms = 0;
    long long grace_ms = 0;
    long long max_log_bytes = 0;
    long long tail_bytes = 0;
    long long drain_after_exit_ms = 0;
    bool stop_on_failure = false;
};

struct VersionRequested {};

struct ParseError {
    int code;
    std::string message;
};

using ParseResult = std::variant<Options, VersionRequested, ParseError>;

// Every value flag is required and has no default. A default would be a fact
// absent from the run record, and the contract makes the command line the
// run's provenance.
ParseResult parse_options(int argc, const char* const* argv);

}  // namespace afrunner
