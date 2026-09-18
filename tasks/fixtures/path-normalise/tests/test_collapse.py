"""Collapsing that stays inside the base. These pass before and after."""

import unittest

from paths.normalise import normalise


class Collapsing(unittest.TestCase):
    def test_a_here_segment_is_dropped(self):
        self.assertEqual(normalise("a/./b"), "a/b")

    def test_a_parent_segment_cancels_the_one_before_it(self):
        # A `..` that stays inside the base is ordinary and must still work.
        # Refusing every `..` is the obvious over-correction.
        self.assertEqual(normalise("a/b/../c"), "a/c")

    def test_cancelling_back_to_the_base_is_allowed(self):
        self.assertEqual(normalise("a/../b"), "b")

    def test_repeated_slashes_collapse(self):
        self.assertEqual(normalise("a//b"), "a/b")

    def test_a_trailing_slash_is_dropped(self):
        self.assertEqual(normalise("a/b/"), "a/b")

    def test_an_ordinary_path_is_unchanged(self):
        self.assertEqual(normalise("a/b/c"), "a/b/c")


class Edges(unittest.TestCase):
    def test_the_empty_path_normalises_to_empty(self):
        self.assertEqual(normalise(""), "")

    def test_a_path_of_only_here_normalises_to_empty(self):
        self.assertEqual(normalise("."), "")

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("/etc/passwd")


if __name__ == "__main__":
    unittest.main()
