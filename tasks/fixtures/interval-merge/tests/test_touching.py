"""Intervals that meet end to start. These fail before the fix."""

import unittest

from intervals.merge import merge


class TouchingIntervals(unittest.TestCase):
    def test_two_touching_intervals_become_one(self):
        self.assertEqual(merge([[1, 3], [3, 5]]), [[1, 5]])

    def test_a_chain_of_touching_intervals_collapses(self):
        self.assertEqual(merge([[1, 2], [2, 3], [3, 4]]), [[1, 4]])

    def test_touching_is_found_after_sorting(self):
        self.assertEqual(merge([[3, 5], [1, 3]]), [[1, 5]])

    def test_a_touching_interval_inside_a_longer_run(self):
        self.assertEqual(merge([[1, 2], [2, 6], [8, 9]]), [[1, 6], [8, 9]])


if __name__ == "__main__":
    unittest.main()
