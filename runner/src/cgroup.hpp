#pragma once

#include <optional>
#include <string>

namespace afrunner {

// The runner's own cgroup. In a container with a private cgroup namespace,
// Docker's default on cgroup v2, this is the container's cgroup.
inline constexpr const char* memory_events_path = "/sys/fs/cgroup/memory.events";

// The `oom_kill` counter of a cgroup v2 memory.events file: how many processes
// in the cgroup the OOM killer has killed. The kernel increments it before it
// sends SIGKILL, so once the victim is reaped the count is final.
//
// Docker's State.OOMKilled reports the same fact through an asynchronous
// event, and under CPU load that event is sometimes lost: the flag stays false
// for good while this counter reads 1. The counter is exact, but it disappears
// with the container, so only a process inside the container can read it in
// time.
//
// Empty when the file cannot be read or holds no valid counter: cgroup v1, or
// a host's root cgroup, which has no memory.events.
std::optional<long long> read_oom_kills(const std::string& events_path);

// {"oom_kills":N}, or {"oom_kills":null} when the counter could not be read.
std::string serialise_oom_kills(std::optional<long long> kills);

}  // namespace afrunner
