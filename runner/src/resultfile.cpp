#include "resultfile.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

namespace afrunner {
namespace {

constexpr const char* temp_suffix = ".afrunner.tmp";

std::string describe(const char* what, const std::string& path, int code) {
    return std::string(what) + ": " + path + ": " + std::strerror(code);
}

std::string parent_of(const std::string& path) {
    const std::size_t slash = path.rfind('/');
    if (slash == std::string::npos) {
        return ".";
    }
    if (slash == 0) {
        return "/";
    }
    return path.substr(0, slash);
}

std::string write_all(int fd, std::string_view text) {
    while (!text.empty()) {
        const ssize_t written = ::write(fd, text.data(), text.size());
        if (written > 0) {
            text.remove_prefix(static_cast<std::size_t>(written));
            continue;
        }
        if (written < 0 && errno == EINTR) {
            continue;
        }
        return std::strerror(errno);
    }
    return {};
}

// A rename is only durable once the directory entry itself is on disk.
std::string sync_directory(const std::string& directory) {
    const int fd = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        return describe("cannot open the output directory", directory, errno);
    }
    const int synced = ::fsync(fd);
    const int code = errno;
    ::close(fd);
    if (synced != 0) {
        return describe("cannot sync the output directory", directory, code);
    }
    return {};
}

}  // namespace

std::string write_atomically(const std::string& path, std::string_view text) {
    const std::string temp = path + temp_suffix;

    const int fd = ::open(temp.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) {
        return describe("cannot create the result file", temp, errno);
    }

    const std::string write_problem = write_all(fd, text);
    if (!write_problem.empty()) {
        ::close(fd);
        ::unlink(temp.c_str());
        return "cannot write the result file: " + temp + ": " + write_problem;
    }

    if (::fsync(fd) != 0) {
        const int code = errno;
        ::close(fd);
        ::unlink(temp.c_str());
        return describe("cannot sync the result file", temp, code);
    }
    if (::close(fd) != 0) {
        const int code = errno;
        ::unlink(temp.c_str());
        return describe("cannot close the result file", temp, code);
    }

    if (::rename(temp.c_str(), path.c_str()) != 0) {
        const int code = errno;
        ::unlink(temp.c_str());
        return describe("cannot move the result into place", path, code);
    }

    return sync_directory(parent_of(path));
}

std::string ensure_directory(const std::string& path) {
    if (path.empty()) {
        return "the directory path is empty";
    }

    // Build the path one component at a time so a missing parent is created
    // too. An existing component is not an error; anything that exists and is
    // not a directory is.
    for (std::size_t at = 1; at <= path.size(); ++at) {
        if (at != path.size() && path[at] != '/') {
            continue;
        }
        const std::string partial = path.substr(0, at);
        if (::mkdir(partial.c_str(), 0755) == 0) {
            continue;
        }
        if (errno != EEXIST) {
            return describe("cannot create the directory", partial, errno);
        }
        struct stat info {};
        if (::stat(partial.c_str(), &info) != 0) {
            return describe("cannot inspect the directory", partial, errno);
        }
        if (!S_ISDIR(info.st_mode)) {
            return "not a directory: " + partial;
        }
    }
    return {};
}

}  // namespace afrunner
