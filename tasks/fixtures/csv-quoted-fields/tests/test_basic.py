"""Unquoted records. These pass before the fix and must pass after it."""

import unittest

from csvkit.fields import split_fields


class SplitPlainRecords(unittest.TestCase):
    def test_three_plain_fields(self):
        self.assertEqual(split_fields("a,b,c"), ["a", "b", "c"])

    def test_single_field(self):
        self.assertEqual(split_fields("solo"), ["solo"])

    def test_empty_record_is_one_empty_field(self):
        self.assertEqual(split_fields(""), [""])

    def test_empty_field_in_the_middle(self):
        self.assertEqual(split_fields("a,,c"), ["a", "", "c"])

    def test_trailing_empty_field_is_kept(self):
        self.assertEqual(split_fields("a,b,"), ["a", "b", ""])

    def test_leading_empty_field_is_kept(self):
        self.assertEqual(split_fields(",b,c"), ["", "b", "c"])


if __name__ == "__main__":
    unittest.main()
