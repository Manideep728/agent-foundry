#include "resultfile.hpp"

#include <gtest/gtest.h>

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>

namespace {

using afrunner::ensure_directory;
using afrunner::write_atomically;

// A directory that removes itself, so a failing test cannot leave litter in
// /tmp for the next run to trip over.
class TempDir {
  public:
    TempDir() {
        std::string pattern = "/tmp/afrunner-test-XXXXXX";
        char* made = ::mkdtemp(pattern.data());
        path_ = made == nullptr ? "" : made;
    }

    ~TempDir() {
        if (!path_.empty()) {
            const std::string command = "rm -rf '" + path_ + "'";
            [[maybe_unused]] const int ignored = std::system(command.c_str());
        }
    }

    TempDir(const TempDir&) = delete;
    TempDir& operator=(const TempDir&) = delete;

    const std::string& path() const { return path_; }
    std::string file(const std::string& name) const { return path_ + "/" + name; }

  private:
    std::string path_;
};

bool exists(const std::string& path) {
    struct stat info {};
    return ::stat(path.c_str(), &info) == 0;
}

std::string contents(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    std::ostringstream buffer;
    buffer << file.rdbuf();
    return buffer.str();
}

TEST(ResultFile, WritesTheContent) {
    const TempDir dir;
    ASSERT_FALSE(dir.path().empty());

    const std::string target = dir.file("result.json");
    EXPECT_EQ(write_atomically(target, R"({"schema_version":1})"), "");
    EXPECT_EQ(contents(target), R"({"schema_version":1})");
}

TEST(ResultFile, LeavesNoTemporaryFileBehind) {
    const TempDir dir;
    const std::string target = dir.file("result.json");

    ASSERT_EQ(write_atomically(target, "{}"), "");
    EXPECT_FALSE(exists(target + ".afrunner.tmp"));
}

TEST(ResultFile, ReplacesAnExistingResult) {
    const TempDir dir;
    const std::string target = dir.file("result.json");

    ASSERT_EQ(write_atomically(target, "{\"first\":1}"), "");
    ASSERT_EQ(write_atomically(target, "{\"second\":2}"), "");
    EXPECT_EQ(contents(target), "{\"second\":2}");
}

TEST(ResultFile, WritesAnEmptyDocument) {
    const TempDir dir;
    const std::string target = dir.file("empty.json");

    EXPECT_EQ(write_atomically(target, ""), "");
    EXPECT_EQ(contents(target), "");
}

// The runner must report this rather than abort, so the control plane sees
// exit 5 and knows the mount is wrong.
TEST(ResultFile, ReportsAMissingDirectory) {
    const std::string problem =
        write_atomically("/nonexistent-afrunner-dir/result.json", "{}");
    EXPECT_NE(problem, "");
}

TEST(ResultFile, ReportsAnUnwritableDirectory) {
    const TempDir dir;
    const std::string locked = dir.file("locked");
    ASSERT_EQ(ensure_directory(locked), "");
    ASSERT_EQ(::chmod(locked.c_str(), 0500), 0);

    const std::string target = locked + "/result.json";
    const std::string problem = write_atomically(target, "{}");

    // Running as root defeats the permission bits, so only assert the
    // behaviour that is actually reachable for this user.
    if (::geteuid() == 0) {
        GTEST_SKIP() << "root ignores the write permission bit";
    }
    EXPECT_NE(problem, "");
    EXPECT_FALSE(exists(target));
}

TEST(ResultFile, CreatesADirectory) {
    const TempDir dir;
    const std::string logs = dir.file("out/logs");

    EXPECT_EQ(ensure_directory(logs), "");
    EXPECT_TRUE(exists(logs));
}

TEST(ResultFile, CreatingAnExistingDirectoryIsNotAnError) {
    const TempDir dir;
    const std::string logs = dir.file("logs");

    ASSERT_EQ(ensure_directory(logs), "");
    EXPECT_EQ(ensure_directory(logs), "");
}

TEST(ResultFile, RefusesADirectoryPathThatIsAFile) {
    const TempDir dir;
    const std::string target = dir.file("occupied");
    ASSERT_EQ(write_atomically(target, "not a directory"), "");

    EXPECT_NE(ensure_directory(target), "");
}

}  // namespace
