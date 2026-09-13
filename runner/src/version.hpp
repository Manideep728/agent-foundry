#pragma once

#include <string_view>

namespace afrunner {

// Mirrors SCHEMA_VERSION in control_plane/domain/validation.py. The reader
// refuses a result whose version it does not recognise, so the two constants
// must move together.
inline constexpr int schema_version = 1;

inline constexpr std::string_view runner_version = "afrunner 0.1.0";

}  // namespace afrunner
