"""Quoted records. These fail until the quoting rules are implemented."""

import unittest

from csvkit.fields import split_fields


class SplitQuotedRecords(unittest.TestCase):
    def test_comma_inside_quotes_is_not_a_separator(self):
        self.assertEqual(split_fields('a,"b,c",d'), ["a", "b,c", "d"])

    def test_doubled_quote_is_a_literal_quote(self):
        self.assertEqual(split_fields('"he said ""hi""",x'), ['he said "hi"', "x"])

    def test_empty_quoted_field(self):
        self.assertEqual(split_fields('"",a'), ["", "a"])

    def test_quotes_are_stripped_from_the_value(self):
        self.assertEqual(split_fields('"plain",b'), ["plain", "b"])

    def test_several_quoted_fields(self):
        self.assertEqual(split_fields('"a,b","c,d"'), ["a,b", "c,d"])


if __name__ == "__main__":
    unittest.main()
