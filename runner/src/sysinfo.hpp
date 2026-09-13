#pragma once

#include <string>

namespace afrunner {

// From uname(2), deliberately not normalised. A benchmark table refuses a row
// produced on the wrong architecture, and accidental qemu emulation has to stay
// visible, so arm64 and x86_64 must not be flattened into one name.
std::string arch();

// Which /bin/sh actually runs the commands, resolved through symlinks. This is
// what records `dash` rather than the `sh` that was asked for.
std::string shell_path();

// Recorded in every result, so the PID-1 obligations are observable in the data
// rather than assumed.
bool is_pid1();

}  // namespace afrunner
