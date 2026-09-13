#include "options.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <string>
#include <variant>
#include <vector>

#include "exit_codes.hpp"

namespace {

using afrunner::Options;
using afrunner::ParseError;
using afrunner::ParseResult;
using afrunner::VersionRequested;

ParseResult parse(const std::vector<std::string>& args) {
    std::vector<std::string> owned;
    owned.reserve(args.size() + 1);
    owned.emplace_back("afrunner");
    owned.insert(owned.end(), args.begin(), args.end());

    std::vector<const char*> argv;
    argv.reserve(owned.size());
    for (const std::string& arg : owned) {
        argv.push_back(arg.c_str());
    }
    return afrunner::parse_options(static_cast<int>(argv.size()), argv.data());
}

// The invocation printed in docs/RUNNER_CONTRACT.md, verbatim.
std::vector<std::string> contract_args() {
    return {
        "--commands",           "/in/commands.txt",
        "--workdir",            "/work",
        "--result",             "/out/result.json",
        "--log-dir",            "/out/logs",
        "--timeout-ms",         "120000",
        "--total-timeout-ms",   "600000",
        "--grace-ms",           "5000",
        "--max-log-bytes",      "8388608",
        "--tail-bytes",         "4096",
        "--drain-after-exit-ms", "2000",
    };
}

std::vector<std::string> args_with(std::string_view flag, std::string_view value) {
    std::vector<std::string> args = contract_args();
    const auto at = std::find(args.begin(), args.end(), flag);
    EXPECT_NE(at, args.end()) << "test refers to a flag that is not in the contract invocation";
    *(at + 1) = std::string(value);
    return args;
}

std::vector<std::string> args_without(std::string_view flag) {
    std::vector<std::string> args = contract_args();
    const auto at = std::find(args.begin(), args.end(), flag);
    EXPECT_NE(at, args.end());
    args.erase(at, at + 2);
    return args;
}

int error_code(const ParseResult& result) {
    const auto* error = std::get_if<ParseError>(&result);
    return error == nullptr ? -1 : error->code;
}

TEST(Options, AcceptsTheContractInvocation) {
    const ParseResult result = parse(contract_args());
    const auto* options = std::get_if<Options>(&result);
    ASSERT_NE(options, nullptr) << "the documented invocation must parse";

    EXPECT_EQ(options->commands, "/in/commands.txt");
    EXPECT_EQ(options->workdir, "/work");
    EXPECT_EQ(options->result, "/out/result.json");
    EXPECT_EQ(options->log_dir, "/out/logs");
    EXPECT_EQ(options->timeout_ms, 120000);
    EXPECT_EQ(options->total_timeout_ms, 600000);
    EXPECT_EQ(options->grace_ms, 5000);
    EXPECT_EQ(options->max_log_bytes, 8388608);
    EXPECT_EQ(options->tail_bytes, 4096);
    EXPECT_EQ(options->drain_after_exit_ms, 2000);
    EXPECT_FALSE(options->stop_on_failure);
}

TEST(Options, StopOnFailureIsOptionalAndBare) {
    std::vector<std::string> args = contract_args();
    args.emplace_back("--stop-on-failure");

    const ParseResult result = parse(args);
    const auto* options = std::get_if<Options>(&result);
    ASSERT_NE(options, nullptr);
    EXPECT_TRUE(options->stop_on_failure);
}

TEST(Options, VersionIsRequestedOnItsOwn) {
    EXPECT_TRUE(std::holds_alternative<VersionRequested>(parse({"--version"})));
}

TEST(Options, VersionAlongsideOtherFlagsIsAUsageError) {
    std::vector<std::string> args = contract_args();
    args.emplace_back("--version");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsAnUnknownFlag) {
    std::vector<std::string> args = contract_args();
    args.emplace_back("--turbo");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsTheEqualsForm) {
    std::vector<std::string> args = args_without("--workdir");
    args.emplace_back("--workdir=/work");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsAPositionalArgument) {
    std::vector<std::string> args = contract_args();
    args.emplace_back("/work");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsARepeatedFlag) {
    std::vector<std::string> args = contract_args();
    args.emplace_back("--workdir");
    args.emplace_back("/elsewhere");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsAFlagWithNoValue) {
    EXPECT_EQ(error_code(parse({"--workdir"})), afrunner::exit_codes::usage);
}

// Without this, "--workdir --result /out/r.json" silently sets the workdir to
// the string "--result" and fails later with a confusing complaint.
TEST(Options, RejectsAValueThatLooksLikeAFlag) {
    std::vector<std::string> args = args_without("--workdir");
    args.emplace_back("--workdir");
    args.emplace_back("--result");
    EXPECT_EQ(error_code(parse(args)), afrunner::exit_codes::usage);
}

TEST(Options, RejectsANonNumericValue) {
    EXPECT_EQ(error_code(parse(args_with("--timeout-ms", "soon"))),
              afrunner::exit_codes::usage);
}

TEST(Options, RejectsATrailingSuffixOnANumber) {
    EXPECT_EQ(error_code(parse(args_with("--timeout-ms", "120000ms"))),
              afrunner::exit_codes::usage);
}

TEST(Options, RejectsANumberThatOverflows) {
    EXPECT_EQ(error_code(parse(args_with("--timeout-ms", "99999999999999999999"))),
              afrunner::exit_codes::usage);
}

TEST(Options, RejectsANonPositiveBudget) {
    for (const std::string_view flag :
         {"--timeout-ms", "--total-timeout-ms", "--max-log-bytes", "--tail-bytes"}) {
        SCOPED_TRACE(flag);
        EXPECT_EQ(error_code(parse(args_with(flag, "0"))), afrunner::exit_codes::usage);
        EXPECT_EQ(error_code(parse(args_with(flag, "-1"))), afrunner::exit_codes::usage);
    }
}

// Zero is meaningful for these two: escalate immediately, and do not wait for
// output once the child is reaped.
TEST(Options, AcceptsAZeroGraceAndDrainWindow) {
    for (const std::string_view flag : {"--grace-ms", "--drain-after-exit-ms"}) {
        SCOPED_TRACE(flag);
        EXPECT_TRUE(std::holds_alternative<Options>(parse(args_with(flag, "0"))));
        EXPECT_EQ(error_code(parse(args_with(flag, "-1"))), afrunner::exit_codes::usage);
    }
}

TEST(Options, RejectsEachMissingRequiredFlag) {
    for (const std::string_view flag :
         {"--commands", "--workdir", "--result", "--log-dir", "--timeout-ms",
          "--total-timeout-ms", "--grace-ms", "--max-log-bytes", "--tail-bytes",
          "--drain-after-exit-ms"}) {
        SCOPED_TRACE(flag);
        EXPECT_EQ(error_code(parse(args_without(flag))), afrunner::exit_codes::usage);
    }
}

TEST(Options, ReportsNoArgumentsAsAUsageError) {
    EXPECT_EQ(error_code(parse({})), afrunner::exit_codes::usage);
}

}  // namespace
