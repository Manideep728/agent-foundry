#include "sysinfo.hpp"

#include <sys/utsname.h>
#include <unistd.h>

#include <cstdlib>

namespace afrunner {

std::string arch() {
    utsname info{};
    if (::uname(&info) != 0) {
        return "unknown";
    }
    return info.machine;
}

std::string shell_path() {
    char* resolved = ::realpath("/bin/sh", nullptr);
    if (resolved == nullptr) {
        return "/bin/sh";
    }
    const std::string path(resolved);
    std::free(resolved);
    return path;
}

bool is_pid1() { return ::getpid() == 1; }

}  // namespace afrunner
