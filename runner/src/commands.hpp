#pragma once

#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace afrunner {

// Every failure here is exit 3, so the error carries only an explanation.
struct CommandFileError {
    std::string message;
};

using CommandList = std::vector<std::string>;
using CommandsResult = std::variant<CommandList, CommandFileError>;

// Splits the text of a command file. A command's identity is its zero-based
// index among the lines that survive, so anything that changes which lines are
// skipped renumbers every command after it.
CommandsResult parse_commands(std::string_view text);

CommandsResult read_commands(const std::string& path);

}  // namespace afrunner
