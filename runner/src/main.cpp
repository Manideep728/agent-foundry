#include <sys/stat.h>

#include <iostream>
#include <string>
#include <string_view>
#include <variant>

#include "clock.hpp"
#include "commands.hpp"
#include "exit_codes.hpp"
#include "options.hpp"
#include "outcome.hpp"
#include "report.hpp"
#include "result.hpp"
#include "resultfile.hpp"
#include "sysinfo.hpp"
#include "version.hpp"

namespace af = afrunner;

namespace {

int fail(const std::string& message, int code) {
    std::cerr << "afrunner: " << message << '\n';
    return code;
}

bool is_directory(const std::string& path) {
    struct stat info {};
    return ::stat(path.c_str(), &info) == 0 && S_ISDIR(info.st_mode);
}

}  // namespace

int main(int argc, char** argv) {
    const af::ParseResult parsed = af::parse_options(argc, argv);

    if (std::holds_alternative<af::VersionRequested>(parsed)) {
        std::cout << af::runner_version << '\n';
        return af::exit_codes::ok;
    }
    if (const auto* error = std::get_if<af::ParseError>(&parsed)) {
        return fail(error->message, error->code);
    }
    const af::Options& options = std::get<af::Options>(parsed);

    const long long started_monotonic = af::monotonic_ms();

    if (!is_directory(options.workdir)) {
        return fail("workdir is missing or not a directory: " + options.workdir,
                    af::exit_codes::workdir);
    }

    const af::CommandsResult commands = af::read_commands(options.commands);
    if (const auto* error = std::get_if<af::CommandFileError>(&commands)) {
        return fail(error->message, af::exit_codes::command_file);
    }
    const af::CommandList& lines = std::get<af::CommandList>(commands);

    if (const std::string problem = af::ensure_directory(options.log_dir);
        !problem.empty()) {
        return fail(problem, af::exit_codes::output);
    }

    af::Report report;
    report.runner_version = std::string(af::runner_version);
    report.arch = af::arch();
    report.pid1 = af::is_pid1();
    report.shell = af::shell_path();
    report.workdir = options.workdir;
    report.started_at_unix_ms = af::unix_ms();

    // Every command in the file gets an entry before any of them runs. The
    // reader requires indices 0..n-1 exactly, so a result that stops early is
    // unparseable unless the unreached entries are already there. Each is
    // overwritten in place as its command completes.
    report.commands.reserve(lines.size());
    for (std::size_t i = 0; i < lines.size(); ++i) {
        af::CommandResult command;
        command.index = static_cast<int>(i);
        command.command = lines[i];
        command.started_at_unix_ms = report.started_at_unix_ms;
        command.verdict = af::not_run();
        report.commands.push_back(std::move(command));
    }

    // Commands do not execute yet. The supervisor fills these entries in; until
    // then every one of them is honestly reported as not_run, which the control
    // plane reads as an incomplete validation rather than as failing tests.

    report.wall_ms = af::monotonic_ms() - started_monotonic;

    if (const std::string problem = af::check_invariants(report); !problem.empty()) {
        return fail("the report violates its own contract: " + problem,
                    af::exit_codes::internal);
    }

    if (const std::string problem =
            af::write_atomically(options.result, af::serialise(report));
        !problem.empty()) {
        return fail(problem, af::exit_codes::output);
    }

    return af::exit_codes::ok;
}
