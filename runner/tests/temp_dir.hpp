#pragma once

#include <unistd.h>

#include <cstdlib>
#include <string>

namespace afrunner::testing {

// A directory that removes itself, so a failing test cannot leave litter in
// /tmp for the next run to trip over.
class TempDir {
  public:
    TempDir() {
        std::string pattern = "/tmp/afrunner-test-XXXXXX";
        const char* made = ::mkdtemp(pattern.data());
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

}  // namespace afrunner::testing
