"""Prerelease precedence. These fail before the fix."""

import unittest
from functools import cmp_to_key

from semver.compare import compare


class PrereleaseSortsBelowTheRelease(unittest.TestCase):
    def test_a_prerelease_is_below_the_release(self):
        self.assertEqual(compare("1.0.0-alpha", "1.0.0"), -1)

    def test_the_release_is_above_its_prerelease(self):
        self.assertEqual(compare("1.0.0", "1.0.0-alpha"), 1)


class PrereleasesAgainstEachOther(unittest.TestCase):
    def test_alphabetic_identifiers_compare_as_text(self):
        self.assertEqual(compare("1.0.0-alpha", "1.0.0-beta"), -1)

    def test_numeric_identifiers_compare_as_numbers(self):
        self.assertEqual(compare("1.0.0-alpha.2", "1.0.0-alpha.10"), -1)

    def test_a_numeric_identifier_sorts_below_an_alphanumeric_one(self):
        self.assertEqual(compare("1.0.0-1", "1.0.0-alpha"), -1)

    def test_fewer_identifiers_sorts_below(self):
        self.assertEqual(compare("1.0.0-alpha", "1.0.0-alpha.1"), -1)


class Sorting(unittest.TestCase):
    def test_a_release_line_sorts_into_order(self):
        versions = ["1.0.0", "1.0.0-beta", "1.0.0-alpha.1", "1.0.0-alpha"]

        self.assertEqual(
            sorted(versions, key=cmp_to_key(compare)),
            ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0"],
        )


if __name__ == "__main__":
    unittest.main()
