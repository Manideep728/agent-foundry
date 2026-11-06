#include "options.hpp"

#include <array>
#include <charconv>
#include <set>
#include <string_view>

#include "exit_codes.hpp"

namespace afrunner {
namespace {

struct StringFlag {
    std::string_view name;
    std::string Options::*field;
};

struct NumberFlag {
    std::string_view name;
    long long Options::*field;
    bool allow_zero;
};

constexpr std::array<StringFlag, 5> string_flags{{
    {"--commands", &Options::commands},
    {"--workdir", &Options::workdir},
    {"--result", &Options::result},
    {"--oom-result", &Options::oom_result},
    {"--log-dir", &Options::log_dir},
}};

// A zero timeout or a zero-byte cap would make the run meaningless, so those
// are usage errors. A zero grace or drain window is meaningful: escalate
// immediately, or do not wait for output after the child is reaped.
constexpr std::array<NumberFlag, 6> number_flags{{
    {"--timeout-ms", &Options::timeout_ms, false},
    {"--total-timeout-ms", &Options::total_timeout_ms, false},
    {"--grace-ms", &Options::grace_ms, true},
    {"--max-log-bytes", &Options::max_log_bytes, false},
    {"--tail-bytes", &Options::tail_bytes, false},
    {"--drain-after-exit-ms", &Options::drain_after_exit_ms, true},
}};

ParseError usage_error(std::string message) {
    return ParseError{exit_codes::usage, std::move(message)};
}

}  // namespace

ParseResult parse_options(int argc, const char* const* argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--version") {
        return VersionRequested{};
    }

    Options options;
    std::set<std::string_view> seen;

    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);

        if (!arg.starts_with("--")) {
            return usage_error("positional arguments are not accepted: " + std::string(arg));
        }
        if (arg.find('=') != std::string_view::npos) {
            return usage_error("use --name value, not --name=value: " + std::string(arg));
        }
        if (!seen.insert(arg).second) {
            return usage_error("repeated flag: " + std::string(arg));
        }

        if (arg == "--stop-on-failure") {
            options.stop_on_failure = true;
            continue;
        }

        // A value is required for every remaining flag. Rejecting one that
        // looks like a flag turns "--workdir --result /out/r.json" into the
        // true diagnosis rather than a confusing complaint about the path.
        const auto take_value = [&](std::string_view& out) {
            if (i + 1 >= argc) {
                return false;
            }
            const std::string_view candidate(argv[i + 1]);
            if (candidate.starts_with("--")) {
                return false;
            }
            out = candidate;
            ++i;
            return true;
        };

        bool matched = false;

        for (const StringFlag& flag : string_flags) {
            if (arg != flag.name) {
                continue;
            }
            std::string_view value;
            if (!take_value(value)) {
                return usage_error("missing value for " + std::string(arg));
            }
            options.*(flag.field) = std::string(value);
            matched = true;
            break;
        }
        if (matched) {
            continue;
        }

        for (const NumberFlag& flag : number_flags) {
            if (arg != flag.name) {
                continue;
            }
            std::string_view value;
            if (!take_value(value)) {
                return usage_error("missing value for " + std::string(arg));
            }
            long long parsed = 0;
            const char* const begin = value.data();
            const char* const end = begin + value.size();
            const auto [stopped, code] = std::from_chars(begin, end, parsed);
            if (code != std::errc{} || stopped != end) {
                return usage_error("not a number: " + std::string(arg) + " " +
                                   std::string(value));
            }
            if (parsed < (flag.allow_zero ? 0 : 1)) {
                return usage_error(std::string(arg) + " must be " +
                                   (flag.allow_zero ? "zero or more" : "greater than zero"));
            }
            options.*(flag.field) = parsed;
            matched = true;
            break;
        }
        if (matched) {
            continue;
        }

        return usage_error("unknown flag: " + std::string(arg));
    }

    for (const StringFlag& flag : string_flags) {
        if (!seen.contains(flag.name)) {
            return usage_error("missing required flag: " + std::string(flag.name));
        }
    }
    for (const NumberFlag& flag : number_flags) {
        if (!seen.contains(flag.name)) {
            return usage_error("missing required flag: " + std::string(flag.name));
        }
    }

    return options;
}

}  // namespace afrunner
