#pragma once

#include <string>
#include <string_view>

#include "result.hpp"
#include "tail.hpp"

namespace afrunner {

// Where one captured stream goes: a log file capped at --max-log-bytes, a tail
// ring holding the last --tail-bytes, and two counters.
//
// Output is capped, never cut off. Once the file is full the runner keeps
// reading and discards the excess, because stopping the read would block the
// child on a full pipe and turn a chatty test into a false timeout.
class Sink {
  public:
    Sink(long long max_log_bytes, std::size_t tail_bytes);
    ~Sink();

    Sink(const Sink&) = delete;
    Sink& operator=(const Sink&) = delete;

    // Creates the log file inside `directory`. Returns an empty string on
    // success. The file is created even for a command that prints nothing, so
    // "produced no output" stays distinguishable from "never ran".
    std::string open(const std::string& directory, const std::string& basename);

    void feed(std::string_view bytes);
    void close();

    StreamCapture capture() const;

  private:
    long long max_log_bytes_;
    TailBuffer tail_;
    int fd_ = -1;
    std::string basename_;
    long long total_ = 0;
    long long written_ = 0;
};

// cmd-000.stdout.log. A bare basename, because the contract says `path` is
// relative to --log-dir and the Python side resolves it as log_dir / path.
std::string log_basename(int index, const char* stream);

}  // namespace afrunner
