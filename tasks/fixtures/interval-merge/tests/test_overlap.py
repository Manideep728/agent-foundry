"""Overlap, disjointness and the edges. These pass before and after."""

import unittest

from intervals.merge import merge


class Overlapping(unittest.TestCase):
    def test_a_contained_interval_disappears(self):
        self.assertEqual(merge([[1, 5], [2, 3]]), [[1, 5]])

    def test_partial_overlap_extends_the_span(self):
        self.assertEqual(merge([[1, 4], [2, 6]]), [[1, 6]])

    def test_overlap_is_found_after_sorting(self):
        self.assertEqual(merge([[5, 7], [1, 6]]), [[1, 7]])


class Disjoint(unittest.TestCase):
    def test_a_gap_keeps_intervals_apart(self):
        self.assertEqual(merge([[1, 2], [5, 6]]), [[1, 2], [5, 6]])

    def test_several_disjoint_intervals_are_only_sorted(self):
        self.assertEqual(merge([[9, 10], [1, 2], [5, 6]]), [[1, 2], [5, 6], [9, 10]])


class Edges(unittest.TestCase):
    def test_no_intervals(self):
        self.assertEqual(merge([]), [])

    def test_a_single_interval_is_returned_unchanged(self):
        self.assertEqual(merge([[1, 2]]), [[1, 2]])

    def test_the_argument_is_not_modified(self):
        given = [[1, 4], [2, 6]]
        merge(given)
        self.assertEqual(given, [[1, 4], [2, 6]])

    def test_the_result_does_not_alias_the_argument(self):
        given = [[1, 2], [5, 6]]
        result = merge(given)
        result[0][1] = 99
        self.assertEqual(given, [[1, 2], [5, 6]])


if __name__ == "__main__":
    unittest.main()
