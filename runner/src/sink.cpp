#include "sink.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstring>

namespace afrunner {

Sink::Sink(long long max_log_bytes, std::size_t tail_bytes)
    : max_log_bytes_(max_log_bytes), tail_(tail_bytes) {}

Sink::~Sink() { close(); }

std::string Sink::open(const std::string& directory, const std::string& basename) {
    const std::string path = directory + "/" + basename;
    fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd_ < 0) {
        return "cannot create the log file: " + path + ": " + std::strerror(errno);
    }
    basename_ = basename;
    return {};
}

void Sink::feed(std::string_view bytes) {
    // Counted and remembered whatever happens to the file, so bytes_total is
    // what the process produced and the tail is the end of all of it.
    total_ += static_cast<long long>(bytes.size());
    tail_.feed(bytes);

    if (fd_ < 0) {
        return;
    }
    const long long room = max_log_bytes_ - written_;
    if (room <= 0) {
        return;
    }

    std::string_view keep = bytes.substr(
        0, std::min<std::size_t>(bytes.size(), static_cast<std::size_t>(room)));
    while (!keep.empty()) {
        const ssize_t put = ::write(fd_, keep.data(), keep.size());
        if (put > 0) {
            written_ += put;
            keep.remove_prefix(static_cast<std::size_t>(put));
            continue;
        }
        if (put < 0 && errno == EINTR) {
            continue;
        }
        // A failing log file must not stop the run: the measurement still
        // exists, and `truncated` reports that the file is not the whole story.
        break;
    }
}

void Sink::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

StreamCapture Sink::capture() const {
    StreamCapture capture;
    capture.bytes_total = total_;
    capture.bytes_written = written_;
    capture.path = basename_;
    capture.tail = tail_.sanitised();
    return capture;
}

std::string log_basename(int index, const char* stream) {
    std::array<char, 64> buffer{};
    std::snprintf(buffer.data(), buffer.size(), "cmd-%03d.%s.log", index, stream);
    return buffer.data();
}

}  // namespace afrunner
