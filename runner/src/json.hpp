#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace afrunner {

// A writer, never a parser. Writing JSON is bounded and every branch is
// testable; reading it is open-ended and is where the memory-safety bugs live,
// so no JSON library is vendored (decision D3).
//
// The caller is responsible for producing a well-formed document: keys belong
// in objects, and every container is closed.
class JsonWriter {
  public:
    JsonWriter& begin_object();
    JsonWriter& end_object();
    JsonWriter& begin_array();
    JsonWriter& end_array();

    JsonWriter& key(std::string_view name);

    JsonWriter& string(std::string_view value);
    JsonWriter& number(long long value);
    JsonWriter& boolean(bool value);
    JsonWriter& null();

    const std::string& text() const { return text_; }

  private:
    void prefix();

    std::string text_;
    std::vector<bool> empty_;
    bool after_key_ = false;
};

}  // namespace afrunner
