#pragma once

namespace afrunner::exit_codes {

// From docs/RUNNER_CONTRACT.md. These describe the runner, not the commands:
// they answer whether result.json can be trusted, never whether a test passed.
//
// 1 is deliberately absent so a generic 1 from a wrapper script stays
// distinguishable from a real runner failure, and 125-127 belong to Docker.
inline constexpr int ok = 0;
inline constexpr int usage = 2;
inline constexpr int command_file = 3;
inline constexpr int workdir = 4;
inline constexpr int output = 5;
inline constexpr int internal = 6;
inline constexpr int aborted = 7;

}  // namespace afrunner::exit_codes
