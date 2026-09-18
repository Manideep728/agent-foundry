"""Reading an entry counts as using it. These fail before the fix."""

import unittest

from lru.cache import LruCache


class ReadingProtectsAnEntry(unittest.TestCase):
    def test_a_read_entry_is_not_the_one_evicted(self):
        cache = LruCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")
        cache.put("c", 3)

        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))

    def test_reading_moves_an_entry_to_the_most_recent_end(self):
        cache = LruCache(3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        cache.get("a")

        self.assertEqual(cache.keys(), ["b", "c", "a"])

    def test_repeated_reads_keep_an_entry_alive(self):
        cache = LruCache(2)
        cache.put("keep", 1)
        cache.put("other", 2)
        for _ in range(3):
            cache.get("keep")
        cache.put("new", 3)

        self.assertEqual(cache.get("keep"), 1)

    def test_the_least_recently_read_entry_goes_first(self):
        cache = LruCache(3)
        for key in ("a", "b", "c"):
            cache.put(key, key)
        cache.get("a")
        cache.get("b")
        cache.put("d", "d")

        self.assertIsNone(cache.get("c"))
        self.assertEqual(cache.get("a"), "a")


if __name__ == "__main__":
    unittest.main()
