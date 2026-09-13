#include "tail.hpp"

#include <gtest/gtest.h>

#include <string>

namespace {

using afrunner::sanitise_ascii;
using afrunner::TailBuffer;

TEST(Tail, KeepsAStreamShorterThanTheWindow) {
    TailBuffer tail(16);
    tail.feed("short");
    EXPECT_EQ(tail.sanitised(), "short");
}

TEST(Tail, KeepsAStreamExactlyTheWindow) {
    TailBuffer tail(5);
    tail.feed("abcde");
    EXPECT_EQ(tail.sanitised(), "abcde");
}

TEST(Tail, KeepsOnlyTheEndOfALongerStream) {
    TailBuffer tail(5);
    tail.feed("abcdefgh");
    EXPECT_EQ(tail.sanitised(), "defgh");
}

TEST(Tail, JoinsManySmallWritesAcrossTheWrap) {
    TailBuffer tail(5);
    for (const char* piece : {"ab", "cd", "ef", "gh"}) {
        tail.feed(piece);
    }
    EXPECT_EQ(tail.sanitised(), "defgh");
}

TEST(Tail, ChunkingDoesNotChangeTheResult) {
    const std::string stream = "the quick brown fox jumps over the lazy dog";

    TailBuffer whole(10);
    whole.feed(stream);

    TailBuffer piecemeal(10);
    for (const char byte : stream) {
        piecemeal.feed(std::string_view(&byte, 1));
    }

    EXPECT_EQ(whole.sanitised(), piecemeal.sanitised());
    EXPECT_EQ(whole.sanitised(), "e lazy dog");
}

// The regression test for the whole design: the log file stops at
// --max-log-bytes and therefore holds the *front* of the output, so a tail
// read back off that file would report the head. Test runners put their
// summary at the end, which is the part scoring reads.
TEST(Tail, TailIsTheEndOfAStreamThatExceededTheCap) {
    TailBuffer tail(32);

    for (int line = 0; line < 5000; ++line) {
        tail.feed("collecting item " + std::to_string(line) + "\n");
    }
    tail.feed("== 3 failed, 411 passed ==\n");

    const std::string result = tail.sanitised();
    EXPECT_EQ(result.size(), 32U);
    EXPECT_NE(result.find("411 passed"), std::string::npos);
    EXPECT_EQ(result.find("collecting"), std::string::npos);
}

TEST(Tail, AnEmptyStreamIsAnEmptyTail) {
    TailBuffer tail(8);
    EXPECT_EQ(tail.sanitised(), "");
    tail.feed("");
    EXPECT_EQ(tail.sanitised(), "");
}

TEST(Sanitise, KeepsPrintableAsciiAndNewlineAndTab) {
    EXPECT_EQ(sanitise_ascii("ok: 1 passed\n\tdone ~!@#"), "ok: 1 passed\n\tdone ~!@#");
}

TEST(Sanitise, ReplacesCarriageReturn) {
    EXPECT_EQ(sanitise_ascii("50%\rdone"), "50%?done");
}

TEST(Sanitise, ReplacesOtherControlAndHighBytes) {
    EXPECT_EQ(sanitise_ascii(std::string_view("a\0b", 3)), "a?b");
    EXPECT_EQ(sanitise_ascii("a\x7F"
                             "b"),
              "a?b");
    EXPECT_EQ(sanitise_ascii("a\xFF"
                             "b"),
              "a?b");
    EXPECT_EQ(sanitise_ascii("caf\xC3\xA9"), "caf??") << "UTF-8 is not ASCII";
}

// A multi-byte replacement would make the tail longer than --tail-bytes and
// turn the ring window into a character window.
TEST(Sanitise, NeverChangesTheLength) {
    const std::string raw("\x00\x01\r\xFF~ a\n", 8);
    EXPECT_EQ(sanitise_ascii(raw).size(), raw.size());
}

}  // namespace
