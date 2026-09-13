#pragma once

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace afrunner {

// Replaces every byte outside printable ASCII, except newline and tab, with
// '?'. One byte becomes one byte, so the tail window stays a byte window and a
// hostile byte from a test cannot produce invalid JSON.
//
// Newline survives because the pytest summary the tail exists for is
// line-structured. Carriage return does not: a progress bar that overwrites
// its own line would otherwise hide the summary.
std::string sanitise_ascii(std::string_view bytes);

// Holds the last `capacity` bytes of a stream.
//
// Fed by every byte read, never by the log file. The file keeps the *front* of
// the output because it stops at --max-log-bytes, while the tail must be the
// *end*, so reading the tail back off a truncated file would report the head.
class TailBuffer {
  public:
    explicit TailBuffer(std::size_t capacity);

    void feed(std::string_view bytes);

    // The retained bytes, oldest first, with sanitise_ascii applied.
    std::string sanitised() const;

  private:
    std::vector<char> buffer_;
    std::size_t next_ = 0;
    bool wrapped_ = false;
};

}  // namespace afrunner
