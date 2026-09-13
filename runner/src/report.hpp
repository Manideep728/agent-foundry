#pragma once

#include <string>

#include "result.hpp"

namespace afrunner {

std::string serialise(const Report& report);

// The same rules control_plane/domain/validation.py enforces in
// __post_init__, checked on the writing side.
//
// Returns an empty string when the report is well formed. Without this, a
// violation is discovered by the control plane refusing the result weeks
// later, and the symptom is a missing measurement rather than a bug here.
std::string check_invariants(const Report& report);

}  // namespace afrunner
