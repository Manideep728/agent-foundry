#include "json.hpp"

#include <gtest/gtest.h>

#include <string>

namespace {

using afrunner::JsonWriter;

std::string quoted(std::string_view value) {
    JsonWriter writer;
    writer.string(value);
    return writer.text();
}

TEST(Json, EscapesTheSevenShortForms) {
    EXPECT_EQ(quoted("a\"b"), R"("a\"b")");
    EXPECT_EQ(quoted("a\\b"), R"("a\\b")");
    EXPECT_EQ(quoted("a\bb"), R"("a\bb")");
    EXPECT_EQ(quoted("a\fb"), R"("a\fb")");
    EXPECT_EQ(quoted("a\nb"), R"("a\nb")");
    EXPECT_EQ(quoted("a\rb"), R"("a\rb")");
    EXPECT_EQ(quoted("a\tb"), R"("a\tb")");
}

// Written with ordinary literals on purpose: a raw string is no protection
// here, because \uXXXX is a universal character name and the compiler still
// turns it into the character it names.
TEST(Json, EscapesOtherControlBytesAsHex) {
    EXPECT_EQ(quoted(std::string_view("\x01", 1)), "\"\\u0001\"");
    EXPECT_EQ(quoted(std::string_view("\x1f", 1)), "\"\\u001f\"");
    EXPECT_EQ(quoted(std::string_view("\0", 1)), "\"\\u0000\"");
}

// Escaping it is legal but noisy, and a test command is full of paths.
TEST(Json, DoesNotEscapeTheSolidus) {
    EXPECT_EQ(quoted("pytest -q tests/"), R"("pytest -q tests/")");
}

TEST(Json, PassesUtf8Through) {
    EXPECT_EQ(quoted("echo \xC3\xA9"), "\"echo \xC3\xA9\"");
}

TEST(Json, WritesScalars) {
    JsonWriter writer;
    writer.begin_array().number(0).number(-1).boolean(true).boolean(false).null().end_array();
    EXPECT_EQ(writer.text(), "[0,-1,true,false,null]");
}

TEST(Json, SeparatesObjectMembers) {
    JsonWriter writer;
    writer.begin_object()
        .key("index")
        .number(0)
        .key("command")
        .string("pytest -q")
        .end_object();
    EXPECT_EQ(writer.text(), R"({"index":0,"command":"pytest -q"})");
}

TEST(Json, WritesAnEmptyObjectAndArray) {
    JsonWriter object;
    object.begin_object().end_object();
    EXPECT_EQ(object.text(), "{}");

    JsonWriter array;
    array.begin_array().end_array();
    EXPECT_EQ(array.text(), "[]");
}

// The shape result.json actually uses: an object holding an array of objects.
TEST(Json, NestsContainersWithoutStraySeparators) {
    JsonWriter writer;
    writer.begin_object()
        .key("schema_version")
        .number(1)
        .key("commands")
        .begin_array()
        .begin_object().key("index").number(0).end_object()
        .begin_object().key("index").number(1).end_object()
        .end_array()
        .key("aborted")
        .boolean(false)
        .end_object();

    EXPECT_EQ(writer.text(),
              R"({"schema_version":1,"commands":[{"index":0},{"index":1}],"aborted":false})");
}

TEST(Json, EscapesKeysToo) {
    JsonWriter writer;
    writer.begin_object().key("a\"b").number(1).end_object();
    EXPECT_EQ(writer.text(), R"({"a\"b":1})");
}

}  // namespace
