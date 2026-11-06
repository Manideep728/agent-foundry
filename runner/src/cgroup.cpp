#include "cgroup.hpp"

#include <charconv>
#include <fstream>
#include <string_view>

#include "json.hpp"

namespace afrunner {

std::optional<long long> read_oom_kills(const std::string& events_path) {
    std::ifstream events(events_path);
    if (!events) {
        return std::nullopt;
    }

    // The key with its separator, so `oom_group_kill` never matches.
    constexpr std::string_view key = "oom_kill ";
    std::string line;
    while (std::getline(events, line)) {
        if (!line.starts_with(key)) {
            continue;
        }
        const char* first = line.data() + key.size();
        const char* last = line.data() + line.size();
        long long count = 0;
        const auto [end, error] = std::from_chars(first, last, count);
        if (error != std::errc{} || end != last || count < 0) {
            return std::nullopt;
        }
        return count;
    }
    return std::nullopt;
}

std::string serialise_oom_kills(std::optional<long long> kills) {
    JsonWriter writer;
    writer.begin_object().key("oom_kills");
    if (kills.has_value()) {
        writer.number(*kills);
    } else {
        writer.null();
    }
    writer.end_object();
    return writer.text();
}

}  // namespace afrunner
