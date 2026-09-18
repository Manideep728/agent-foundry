"""Paths that climb out of the base. These fail before the fix."""

import unittest

from paths.normalise import normalise


class EscapingIsRefused(unittest.TestCase):
    def test_a_leading_parent_segment_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("../x")

    def test_climbing_past_the_base_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("a/../../x")

    def test_a_bare_parent_segment_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("..")

    def test_climbing_further_than_the_depth_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("a/b/../../..")

    def test_a_parent_segment_after_here_is_refused(self):
        with self.assertRaises(ValueError):
            normalise("./../x")

    def test_escaping_is_refused_rather_than_resolved(self):
        # The dangerous failure is not an exception, it is a plausible answer:
        # returning "x" for "../x" says the path is inside the base when it is
        # not, and every caller downstream then trusts it.
        for path in ("../x", "a/../../x", "../../etc/passwd"):
            with self.assertRaises(ValueError):
                normalise(path)


if __name__ == "__main__":
    unittest.main()
