#include <sys/stat.h>

#include <iostream>
#include <string>
#include <variant>

#include "cgroup.hpp"
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

    // Both before any fork. The blocked mask is inherited, and every orphaned
    // descendant must reparent here so it can be reaped. Inside the container
    // the process is already PID 1, where become_subreaper changes nothing and
    // blocking is the only way a signal is heard at all: PID 1 discards a
    // signal whose disposition is still SIG_DFL.
    af::become_subreaper();
    if (const std::string problem = af::block_signals(); !problem.empty()) {
        return fail(problem, af::exit_codes::internal);
    }
    const int signal_fd = af::make_signalfd();
    if (signal_fd < 0) {
        return fail("cannot watch for signals", af::exit_codes::internal);
    }
    af::AbortWatch watch(signal_fd);

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

    af::RunAll all = af::run_all(options, lines, watch);
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

    // Before result.json, whose presence says the runner finished, so a reader
    // that finds the result can rely on this file too. Read after every
    // command and orphan is reaped, so the count covers the whole run.
    if (const std::string problem = af::write_atomically(
            options.oom_result,
            af::serialise_oom_kills(af::read_oom_kills(af::memory_events_path)));
        !problem.empty()) {
        return fail(problem, af::exit_codes::output);
    }

    // Written even when the run was cut short. A partial result names every
    // command in the file, with the unreached ones not_run, because the reader
    // requires indices 0..n-1 and would refuse anything shorter.
    if (const std::string problem =
            af::write_atomically(options.result, af::serialise(all.report));
        !problem.empty()) {
        return fail(problem, af::exit_codes::output);
    }

    // The exit code describes the runner, not the commands. A run in which
    // every command failed still exits 0, because the measurement is
    // trustworthy; whether validation passed is inside result.json.
    if (all.report.exit_reason == af::ExitReason::Signal) {
        return af::exit_codes::aborted;
    }
    return af::exit_codes::ok;
}
