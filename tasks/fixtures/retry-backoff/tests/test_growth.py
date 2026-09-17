"""Doubling, counting and refusal. These pass before and after."""

import unittest

from retry.backoff import delays


class Doubling(unittest.TestCase):
    def test_each_wait_doubles_the_one_before(self):
        self.assertEqual(delays(4, 100, 10000), [100, 200, 400])

    def test_the_first_wait_is_the_base(self):
        self.assertEqual(delays(2, 50, 10000), [50])


class Counting(unittest.TestCase):
    def test_a_single_attempt_waits_not_at_all(self):
        self.assertEqual(delays(1, 100, 1000), [])

    def test_the_waits_go_between_the_attempts(self):
        self.assertEqual(len(delays(5, 10, 100000)), 4)


class Refusal(unittest.TestCase):
    def test_zero_attempts_is_refused(self):
        with self.assertRaises(ValueError):
            delays(0, 100, 1000)

    def test_a_base_of_zero_is_refused(self):
        with self.assertRaises(ValueError):
            delays(3, 0, 1000)

    def test_a_negative_cap_is_refused(self):
        with self.assertRaises(ValueError):
            delays(3, 100, -1)


class WholeMilliseconds(unittest.TestCase):
    def test_every_wait_is_an_integer(self):
        for wait in delays(6, 100, 100000):
            self.assertIsInstance(wait, int)


if __name__ == "__main__":
    unittest.main()
