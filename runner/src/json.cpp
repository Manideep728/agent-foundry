#include "json.hpp"

#include <string>

namespace afrunner {
namespace {

void write_escaped(std::string& out, std::string_view value) {
    static constexpr char hex[] = "0123456789abcdef";

    out += '"';
    for (const char raw : value) {
        const auto byte = static_cast<unsigned char>(raw);
        switch (byte) {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\b':
                out += "\\b";
                break;
            case '\f':
                out += "\\f";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                if (byte < 0x20) {
                    out += "\\u00";
                    out += hex[byte >> 4];
                    out += hex[byte & 0x0F];
                } else {
                    // Bytes at or above 0x80 pass through: the input is
                    // already valid UTF-8, and JSON carries it unescaped.
                    out += raw;
                }
        }
    }
    out += '"';
}

}  // namespace

void JsonWriter::prefix() {
    // A value that follows a key is that key's value, never a sibling.
    if (after_key_) {
        after_key_ = false;
        return;
    }
    if (empty_.empty()) {
        return;
    }
    if (!empty_.back()) {
        text_ += ',';
    }
    empty_.back() = false;
}

JsonWriter& JsonWriter::begin_object() {
    prefix();
    text_ += '{';
    empty_.push_back(true);
    return *this;
}

JsonWriter& JsonWriter::end_object() {
    empty_.pop_back();
    text_ += '}';
    return *this;
}

JsonWriter& JsonWriter::begin_array() {
    prefix();
    text_ += '[';
    empty_.push_back(true);
    return *this;
}

JsonWriter& JsonWriter::end_array() {
    empty_.pop_back();
    text_ += ']';
    return *this;
}

JsonWriter& JsonWriter::key(std::string_view name) {
    prefix();
    write_escaped(text_, name);
    text_ += ':';
    after_key_ = true;
    return *this;
}

JsonWriter& JsonWriter::string(std::string_view value) {
    prefix();
    write_escaped(text_, value);
    return *this;
}

JsonWriter& JsonWriter::number(long long value) {
    prefix();
    text_ += std::to_string(value);
    return *this;
}

JsonWriter& JsonWriter::boolean(bool value) {
    prefix();
    text_ += value ? "true" : "false";
    return *this;
}

JsonWriter& JsonWriter::null() {
    prefix();
    text_ += "null";
    return *this;
}

}  // namespace afrunner
