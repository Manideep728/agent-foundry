"""Numeric ordering and parsing. These pass before and after."""

import unittest

from semver.compare import compare, parse


class NumericOrdering(unittest.TestCase):
    def test_patch_decides_when_major_and_minor_match(self):
        self.assertEqual(compare("1.0.0", "1.0.1"), -1)

    def test_minor_outranks_patch(self):
        self.assertEqual(compare("1.1.0", "1.0.9"), 1)

    def test_major_outranks_everything(self):
        self.assertEqual(compare("2.0.0", "1.9.9"), 1)

    def test_ten_is_above_nine_rather_than_below_it(self):
        # The comparison is numeric, not lexicographic. As text "1.10.0" sorts
        # below "1.9.0", which is the classic way to get this wrong.
        self.assertEqual(compare("1.10.0", "1.9.0"), 1)

    def test_identical_versions_compare_equal(self):
        self.assertEqual(compare("1.2.3", "1.2.3"), 0)

    def test_identical_prereleases_compare_equal(self):
        self.assertEqual(compare("1.0.0-alpha", "1.0.0-alpha"), 0)


class Parsing(unittest.TestCase):
    def test_the_numbers_come_back_as_integers(self):
        self.assertEqual(parse("1.2.3"), (1, 2, 3, None))

    def test_a_prerelease_is_kept_as_text(self):
        self.assertEqual(parse("1.0.0-alpha.1"), (1, 0, 0, "alpha.1"))

    def test_something_that_is_not_a_version_is_refused(self):
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "", "1.0.x"):
            with self.assertRaises(ValueError):
                parse(bad)


if __name__ == "__main__":
    unittest.main()
