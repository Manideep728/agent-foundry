#include "commands.hpp"

#include <gtest/gtest.h>

#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace {

using afrunner::CommandFileError;
using afrunner::CommandList;
using afrunner::CommandsResult;

CommandList commands_of(std::string_view text) {
    const CommandsResult result = afrunner::parse_commands(text);
    const auto* list = std::get_if<CommandList>(&result);
    EXPECT_NE(list, nullptr) << "expected commands, got an error";
    return list == nullptr ? CommandList{} : *list;
}

bool is_error(std::string_view text) {
    return std::holds_alternative<CommandFileError>(afrunner::parse_commands(text));
}

TEST(Commands, KeepsOrderAndOneCommandPerLine) {
    EXPECT_EQ(commands_of("pytest -q\nruff check .\n"),
              (CommandList{"pytest -q", "ruff check ."}));
}

TEST(Commands, AcceptsAFinalLineWithNoNewline) {
    EXPECT_EQ(commands_of("pytest -q"), (CommandList{"pytest -q"}));
}

TEST(Commands, SkipsBlankLines) {
    EXPECT_EQ(commands_of("\n\npytest -q\n\nruff check .\n"),
              (CommandList{"pytest -q", "ruff check ."}));
}

TEST(Commands, SkipsLinesWhoseFirstByteIsAHash) {
    EXPECT_EQ(commands_of("# the fail-to-pass suite\npytest -q\n"),
              (CommandList{"pytest -q"}));
}

// The contract says "first byte", not "first non-space byte". An indented hash
// is a command, and the runner must not be cleverer than the document.
TEST(Commands, AnIndentedHashIsACommandNotAComment) {
    EXPECT_EQ(commands_of("  # not a comment\n"), (CommandList{"  # not a comment"}));
}

TEST(Commands, AHashInsideALineIsNotAComment) {
    EXPECT_EQ(commands_of("echo '# 1'\n"), (CommandList{"echo '# 1'"}));
}

TEST(Commands, StripsTheTrailingCarriageReturn) {
    EXPECT_EQ(commands_of("pytest -q tests/\r\nruff check .\r\n"),
              (CommandList{"pytest -q tests/", "ruff check ."}));
}

TEST(Commands, KeepsACarriageReturnThatIsNotAtTheEnd) {
    EXPECT_EQ(commands_of("printf 'a\rb'\n"), (CommandList{"printf 'a\rb'"}));
}

TEST(Commands, ACarriageReturnOnlyLineIsBlank) {
    EXPECT_EQ(commands_of("pytest -q\n\r\nruff check .\n"),
              (CommandList{"pytest -q", "ruff check ."}));
}

// Indices are positions among the surviving lines, so a skipped line must not
// leave a gap. This is the property the whole index-to-role mapping rests on.
TEST(Commands, IndicesCountOnlySurvivingLines) {
    const CommandList commands = commands_of("# header\n\nfirst\n\n# middle\nsecond\n");
    ASSERT_EQ(commands.size(), 2U);
    EXPECT_EQ(commands[0], "first");
    EXPECT_EQ(commands[1], "second");
}

TEST(Commands, AnEmptyFileIsAnError) {
    EXPECT_TRUE(is_error(""));
}

TEST(Commands, AFileOfOnlyBlankLinesIsAnError) {
    EXPECT_TRUE(is_error("\n\n\n"));
}

TEST(Commands, AFileOfOnlyCommentsIsAnError) {
    EXPECT_TRUE(is_error("# nothing to do\n# really\n"));
}

TEST(Commands, AcceptsMultiByteUtf8) {
    EXPECT_EQ(commands_of("echo \xC3\xA9\n"), (CommandList{"echo \xC3\xA9"}));
}

TEST(Commands, RejectsInvalidUtf8) {
    EXPECT_TRUE(is_error("echo \xFF\n")) << "a lone 0xFF is not a UTF-8 lead byte";
    EXPECT_TRUE(is_error("echo \xC3\n")) << "a truncated two-byte sequence";
    EXPECT_TRUE(is_error("echo \xC0\xAF\n")) << "an overlong encoding of '/'";
    EXPECT_TRUE(is_error("echo \xED\xA0\x80\n")) << "a surrogate half";
}

TEST(Commands, ReadingAMissingFileIsAnError) {
    const CommandsResult result = afrunner::read_commands("/nonexistent/commands.txt");
    EXPECT_TRUE(std::holds_alternative<CommandFileError>(result));
}

TEST(Commands, ReadingADirectoryIsAnError) {
    const CommandsResult result = afrunner::read_commands("/tmp");
    EXPECT_TRUE(std::holds_alternative<CommandFileError>(result));
}

}  // namespace
