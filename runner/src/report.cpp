#include "report.hpp"

#include <string>

#include "json.hpp"
#include "version.hpp"

namespace afrunner {
namespace {

bool is_blank(const std::string& text) {
    for (const char byte : text) {
        if (byte != ' ' && byte != '\t' && byte != '\n' && byte != '\r') {
            return false;
        }
    }
    return true;
}

void write_stream(JsonWriter& writer, const StreamCapture& stream) {
    writer.begin_object()
        .key("bytes_total")
        .number(stream.bytes_total)
        .key("bytes_written")
        .number(stream.bytes_written)
        .key("truncated")
        .boolean(stream.bytes_written < stream.bytes_total)
        .key("path")
        .string(stream.path)
        .key("tail")
        .string(stream.tail)
        .end_object();
}

void write_optional(JsonWriter& writer, const std::optional<int>& value) {
    if (value.has_value()) {
        writer.number(*value);
    } else {
        writer.null();
    }
}

void write_command(JsonWriter& writer, const CommandResult& command) {
    const Verdict& verdict = command.verdict;

    writer.begin_object()
        .key("index")
        .number(command.index)
        .key("command")
        .string(command.command)
        .key("started_at_unix_ms")
        .number(command.started_at_unix_ms)
        .key("duration_ms")
        .number(command.duration_ms)
        .key("outcome")
        .string(to_string(verdict.outcome));

    writer.key("exit_code");
    write_optional(writer, verdict.exit_code);
    writer.key("signal");
    write_optional(writer, verdict.signal);

    writer.key("timed_out")
        .boolean(timed_out(verdict))
        .key("deadline_signal_sent")
        .boolean(verdict.deadline_signal_sent)
        .key("killed_after_grace")
        .boolean(verdict.killed_after_grace)
        .key("group_escaped")
        .boolean(verdict.group_escaped)
        .key("drain_deadline_hit")
        .boolean(command.drain_deadline_hit)
        .key("orphans_reaped")
        .number(command.orphans_reaped);

    writer.key("spawn_error");
    if (verdict.outcome == Outcome::SpawnFailed) {
        writer.string(command.spawn_error);
    } else {
        writer.null();
    }

    writer.key("stdout");
    write_stream(writer, command.out);
    writer.key("stderr");
    write_stream(writer, command.err);

    writer.end_object();
}

std::string check_stream(const StreamCapture& stream, int index, const char* name) {
    const std::string where =
        "command " + std::to_string(index) + " " + name + ": ";
    if (stream.bytes_total < 0 || stream.bytes_written < 0) {
        return where + "byte counts must be >= 0";
    }
    if (stream.bytes_written > stream.bytes_total) {
        return where + "bytes_written exceeds bytes_total";
    }
    return {};
}

std::string check_command(const CommandResult& command) {
    const Verdict& verdict = command.verdict;
    const std::string where = "command " + std::to_string(command.index) + ": ";

    if (command.index < 0) {
        return where + "index must be >= 0";
    }
    if (command.duration_ms < 0) {
        return where + "duration_ms must be >= 0";
    }
    if (command.orphans_reaped < 0) {
        return where + "orphans_reaped must be >= 0";
    }
    if (verdict.exit_code.has_value() != (verdict.outcome == Outcome::Exited)) {
        return where + "exit_code must be set exactly when the outcome is exited";
    }
    const bool wants_signal =
        verdict.outcome == Outcome::Signaled || verdict.outcome == Outcome::Timeout;
    if (verdict.signal.has_value() != wants_signal) {
        return where + "signal must be set exactly when the outcome is signaled or timeout";
    }
    if (command.spawn_error.empty() == (verdict.outcome == Outcome::SpawnFailed)) {
        return where + "spawn_error must be set exactly when the outcome is spawn_failed";
    }
    if (verdict.outcome == Outcome::Timeout && !verdict.deadline_signal_sent) {
        return where + "timed out without the deadline having fired";
    }
    if (verdict.killed_after_grace && !verdict.deadline_signal_sent) {
        return where + "killed after grace without the deadline having fired";
    }

    const std::string out = check_stream(command.out, command.index, "stdout");
    if (!out.empty()) {
        return out;
    }
    return check_stream(command.err, command.index, "stderr");
}

}  // namespace

const char* to_string(ExitReason reason) {
    switch (reason) {
        case ExitReason::TotalTimeout:
            return "total_timeout";
        case ExitReason::Signal:
            return "signal";
        case ExitReason::Completed:
            break;
    }
    return "completed";
}

std::string check_invariants(const Report& report) {
    if (is_blank(report.arch)) {
        return "arch must be recorded";
    }
    if (report.wall_ms < 0) {
        return "wall_ms must be >= 0";
    }
    if (report.orphans_reaped_total < 0) {
        return "orphans_reaped_total must be >= 0";
    }
    if (report.commands.empty()) {
        return "a report must contain at least one command";
    }

    for (std::size_t i = 0; i < report.commands.size(); ++i) {
        if (report.commands[i].index != static_cast<int>(i)) {
            return "command indices must be exactly 0..n-1, in order";
        }
        const std::string problem = check_command(report.commands[i]);
        if (!problem.empty()) {
            return problem;
        }
    }
    return {};
}

std::string serialise(const Report& report) {
    JsonWriter writer;

    writer.begin_object()
        .key("schema_version")
        .number(schema_version)
        .key("runner_version")
        .string(report.runner_version)
        .key("arch")
        .string(report.arch)
        .key("pid1")
        .boolean(report.pid1)
        .key("shell")
        .string(report.shell)
        .key("workdir")
        .string(report.workdir)
        .key("started_at_unix_ms")
        .number(report.started_at_unix_ms)
        .key("wall_ms")
        .number(report.wall_ms)
        .key("exit_reason")
        .string(to_string(report.exit_reason))
        .key("aborted")
        .boolean(report.exit_reason != ExitReason::Completed)
        .key("orphans_reaped_total")
        .number(report.orphans_reaped_total);

    writer.key("commands").begin_array();
    for (const CommandResult& command : report.commands) {
        write_command(writer, command);
    }
    writer.end_array();

    writer.end_object();
    return writer.text();
}

}  // namespace afrunner
