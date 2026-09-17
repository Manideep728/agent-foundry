"""Counts that divide evenly, and the edges. These pass before and after."""

import unittest

from paging.pages import paginate


class ExactMultiples(unittest.TestCase):
    def test_two_full_pages(self):
        self.assertEqual(paginate([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_one_full_page(self):
        self.assertEqual(paginate([1, 2], 2), [[1, 2]])

    def test_an_exact_fit_produces_no_trailing_empty_page(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5, 6], 3), [[1, 2, 3], [4, 5, 6]])


class Edges(unittest.TestCase):
    def test_no_items_means_no_pages(self):
        self.assertEqual(paginate([], 3), [])

    def test_a_page_size_of_zero_is_refused(self):
        with self.assertRaises(ValueError):
            paginate([1, 2, 3], 0)

    def test_a_negative_page_size_is_refused(self):
        with self.assertRaises(ValueError):
            paginate([1, 2, 3], -1)


if __name__ == "__main__":
    unittest.main()
