#include "tail.hpp"

#include <algorithm>

namespace afrunner {

std::string sanitise_ascii(std::string_view bytes) {
    std::string out;
    out.reserve(bytes.size());
    for (const char raw : bytes) {
        const auto byte = static_cast<unsigned char>(raw);
        const bool printable = byte >= 0x20 && byte <= 0x7E;
        out += (printable || byte == '\n' || byte == '\t') ? raw : '?';
    }
    return out;
}

TailBuffer::TailBuffer(std::size_t capacity) : buffer_(capacity) {}

void TailBuffer::feed(std::string_view bytes) {
    if (buffer_.empty() || bytes.empty()) {
        return;
    }

    // A single write larger than the window overwrites all of it, so only its
    // own last `capacity` bytes can survive.
    if (bytes.size() >= buffer_.size()) {
        bytes.remove_prefix(bytes.size() - buffer_.size());
        std::copy(bytes.begin(), bytes.end(), buffer_.begin());
        next_ = 0;
        wrapped_ = true;
        return;
    }

    for (const char byte : bytes) {
        buffer_[next_] = byte;
        ++next_;
        if (next_ == buffer_.size()) {
            next_ = 0;
            wrapped_ = true;
        }
    }
}

std::string TailBuffer::sanitised() const {
    std::string raw;
    if (wrapped_) {
        raw.reserve(buffer_.size());
        raw.append(buffer_.begin() + static_cast<std::ptrdiff_t>(next_), buffer_.end());
        raw.append(buffer_.begin(), buffer_.begin() + static_cast<std::ptrdiff_t>(next_));
    } else {
        raw.append(buffer_.begin(), buffer_.begin() + static_cast<std::ptrdiff_t>(next_));
    }
    return sanitise_ascii(raw);
}

}  // namespace afrunner
