#include "commands.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

namespace afrunner {
namespace {

// The command is copied verbatim into result.json, so a malformed byte here
// would become malformed JSON that the control plane cannot parse. Refusing
// the file names the real problem instead.
bool is_valid_utf8(std::string_view text) {
    const auto* cursor = reinterpret_cast<const unsigned char*>(text.data());
    const auto* const end = cursor + text.size();

    while (cursor < end) {
        const unsigned char lead = *cursor;
        if (lead < 0x80) {
            ++cursor;
            continue;
        }

        int length = 0;
        unsigned int point = 0;
        if ((lead & 0xE0) == 0xC0) {
            length = 2;
            point = lead & 0x1FU;
        } else if ((lead & 0xF0) == 0xE0) {
            length = 3;
            point = lead & 0x0FU;
        } else if ((lead & 0xF8) == 0xF0) {
            length = 4;
            point = lead & 0x07U;
        } else {
            return false;
        }

        if (end - cursor < length) {
            return false;
        }
        for (int i = 1; i < length; ++i) {
            const unsigned char continuation = cursor[i];
            if ((continuation & 0xC0) != 0x80) {
                return false;
            }
            point = (point << 6) | (continuation & 0x3FU);
        }

        // An overlong encoding, a surrogate half, or a point above the Unicode
        // maximum are all well-formed-looking byte sequences that no encoder
        // should produce.
        static constexpr std::array<unsigned int, 5> smallest{0, 0, 0x80, 0x800, 0x10000};
        if (point < smallest[static_cast<std::size_t>(length)] || point > 0x10FFFF ||
            (point >= 0xD800 && point <= 0xDFFF)) {
            return false;
        }

        cursor += length;
    }
    return true;
}

}  // namespace

CommandsResult parse_commands(std::string_view text) {
    if (!is_valid_utf8(text)) {
        return CommandFileError{"the command file is not valid UTF-8"};
    }

    CommandList commands;
    std::size_t start = 0;
    while (true) {
        const std::size_t newline = text.find('\n', start);
        std::string_view line = newline == std::string_view::npos
                                    ? text.substr(start)
                                    : text.substr(start, newline - start);

        // A task file authored on Windows arrives as \r\n, and `pytest -q
        // tests/\r` fails with a confusing "no such file".
        if (line.ends_with('\r')) {
            line.remove_suffix(1);
        }

        // The contract says "lines whose first byte is #", so an indented hash
        // is a command. Being literal keeps the runner and the control plane
        // agreeing on which lines exist, which is what the indices count.
        if (!line.empty() && line.front() != '#') {
            commands.emplace_back(line);
        }

        if (newline == std::string_view::npos) {
            break;
        }
        start = newline + 1;
    }

    // A report cannot represent zero commands: ValidationReport refuses an
    // empty list. An exit code that says why beats a result the reader must
    // refuse.
    if (commands.empty()) {
        return CommandFileError{"the command file contains no commands"};
    }
    return commands;
}

// Read with POSIX calls rather than ifstream. libstdc++ throws
// std::ios_base::failure out of basic_filebuf::underflow when the path is a
// directory, which would abort the runner instead of returning exit 3.
CommandsResult read_commands(const std::string& path) {
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return CommandFileError{"cannot open the command file: " + path + ": " +
                                std::strerror(errno)};
    }

    const auto fail = [&](const char* what, int code) {
        ::close(fd);
        return CommandFileError{std::string(what) + ": " + path + ": " + std::strerror(code)};
    };

    struct stat info {};
    if (::fstat(fd, &info) != 0) {
        return fail("cannot inspect the command file", errno);
    }
    if (!S_ISREG(info.st_mode)) {
        ::close(fd);
        return CommandFileError{"the command file is not a regular file: " + path};
    }

    std::string text;
    std::array<char, 65536> buffer{};
    while (true) {
        const ssize_t got = ::read(fd, buffer.data(), buffer.size());
        if (got > 0) {
            text.append(buffer.data(), static_cast<std::size_t>(got));
            continue;
        }
        if (got == 0) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        return fail("cannot read the command file", errno);
    }

    ::close(fd);
    return parse_commands(text);
}

}  // namespace afrunner
