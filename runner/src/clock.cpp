#include "clock.hpp"

#include <ctime>

namespace afrunner {
namespace {

long long milliseconds_of(clockid_t which) {
    timespec now{};
    if (::clock_gettime(which, &now) != 0) {
        return 0;
    }
    return static_cast<long long>(now.tv_sec) * 1000 + now.tv_nsec / 1000000;
}

}  // namespace

long long unix_ms() { return milliseconds_of(CLOCK_REALTIME); }

long long monotonic_ms() { return milliseconds_of(CLOCK_MONOTONIC); }

}  // namespace afrunner
