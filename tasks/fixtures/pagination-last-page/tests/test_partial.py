"""Counts that do not divide evenly. These fail before the fix."""

import unittest

from paging.pages import paginate


class PartialFinalPage(unittest.TestCase):
    def test_remainder_becomes_a_final_short_page(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_fewer_items_than_one_page(self):
        self.assertEqual(paginate([1, 2, 3], 5), [[1, 2, 3]])

    def test_remainder_of_one_is_not_dropped(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5, 6, 7], 3), [[1, 2, 3], [4, 5, 6], [7]])

    def test_every_entry_survives_pagination(self):
        items = list(range(17))
        flattened = [entry for page in paginate(items, 5) for entry in page]
        self.assertEqual(flattened, items)


if __name__ == "__main__":
    unittest.main()
