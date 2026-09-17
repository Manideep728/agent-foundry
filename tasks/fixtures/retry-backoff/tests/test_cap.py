"""The ceiling on a wait. These fail before the fix."""

import unittest

from retry.backoff import delays


class CappedWaits(unittest.TestCase):
    def test_doubling_stops_at_the_cap(self):
        self.assertEqual(delays(6, 100, 500), [100, 200, 400, 500, 500])

    def test_no_wait_exceeds_the_cap(self):
        for wait in delays(12, 10, 1000):
            self.assertLessEqual(wait, 1000)

    def test_a_cap_below_the_base_caps_every_wait(self):
        self.assertEqual(delays(3, 1000, 250), [250, 250])

    def test_the_cap_holds_for_a_long_schedule(self):
        self.assertEqual(delays(8, 100, 400), [100, 200, 400, 400, 400, 400, 400])


if __name__ == "__main__":
    unittest.main()
