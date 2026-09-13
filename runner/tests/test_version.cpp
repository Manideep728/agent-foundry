#include "version.hpp"

#include <gtest/gtest.h>

namespace {

// control_plane/domain/validation.py refuses a result whose schema_version it
// does not recognise, so this constant drifting is a silent wire break.
TEST(Version, SchemaVersionMatchesTheContract) {
    EXPECT_EQ(afrunner::schema_version, 1);
}

TEST(Version, RunnerVersionNamesTheRunner) {
    EXPECT_TRUE(afrunner::runner_version.starts_with("afrunner "));
}

}  // namespace
