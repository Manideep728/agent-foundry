#include <sys/stat.h>

#include <iostream>
#include <string>
#include <variant>

#include "commands.hpp"
#include "exit_codes.hpp"
#include "options.hpp"
#include "report.hpp"
#include "result.hpp"
#include "resultfile.hpp"
#include "signals.hpp"
#include "supervisor.hpp"
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

    // Before any fork, so every orphaned descendant reparents here and can be
    // reaped. Inside the container the process is already PID 1 and this
    // changes nothing.
    af::become_subreaper();

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

    af::RunAll all = af::run_all(options, lines);
    if (!all.fatal.empty()) {
        return fail(all.fatal, af::exit_codes::output);
    }

    // The same rules the control plane enforces when it reads the file.
    // Failing here turns "the result was silently refused three weeks later"
    // into an error that names the field.
    if (const std::string problem = af::check_invariants(all.report);
        !problem.empty()) {
        return fail("the report violates its own contract: " + problem,
                    af::exit_codes::internal);
    }

    if (const std::string problem =
            af::write_atomically(options.result, af::serialise(all.report));
        !problem.empty()) {
        return fail(problem, af::exit_codes::output);
    }

    // The exit code describes the runner, not the commands. A run in which
    // every command failed still exits 0, because the measurement is
    // trustworthy; whether validation passed is inside result.json.
    return af::exit_codes::ok;
}
