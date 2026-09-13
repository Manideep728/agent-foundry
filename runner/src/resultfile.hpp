#pragma once

#include <string>
#include <string_view>

namespace afrunner {

// Writes to a temporary name beside the target, fsyncs it, renames it into
// place, then fsyncs the directory.
//
// The absence of the result file has to stay meaningful: it says the runner did
// not finish. A half-written file would be ambiguous, and the control plane
// would have to guess whether a short read was a crash or a slow mount.
//
// This is atomic on ext4. The contract deliberately claims no more than that,
// so the reader still treats a missing, truncated or unparseable result as a
// runner failure.
//
// Returns an empty string on success, or the reason for failure.
std::string write_atomically(const std::string& path, std::string_view text);

// Creates the directory and any missing parent, like `mkdir -p`.
std::string ensure_directory(const std::string& path);

}  // namespace afrunner
