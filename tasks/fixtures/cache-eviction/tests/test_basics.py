"""Storing, retrieving and evicting. These pass before and after."""

import unittest

from lru.cache import LruCache


class StoreAndRetrieve(unittest.TestCase):
    def test_a_stored_value_comes_back(self):
        cache = LruCache(2)
        cache.put("a", 1)

        self.assertEqual(cache.get("a"), 1)

    def test_an_absent_key_returns_the_default(self):
        cache = LruCache(2)

        self.assertIsNone(cache.get("missing"))
        self.assertEqual(cache.get("missing", "fallback"), "fallback")

    def test_reading_an_absent_key_stores_nothing(self):
        cache = LruCache(2)
        cache.get("missing")

        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.keys(), [])

    def test_storing_the_same_key_twice_updates_it(self):
        cache = LruCache(2)
        cache.put("a", 1)
        cache.put("a", 2)

        self.assertEqual(cache.get("a"), 2)
        self.assertEqual(len(cache), 1)


class Eviction(unittest.TestCase):
    def test_the_cache_never_exceeds_its_capacity(self):
        cache = LruCache(2)
        for key in ("a", "b", "c", "d"):
            cache.put(key, key)

        self.assertEqual(len(cache), 2)

    def test_insertion_order_decides_when_nothing_was_read(self):
        cache = LruCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)

        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)


class Refusal(unittest.TestCase):
    def test_a_capacity_of_zero_is_refused(self):
        with self.assertRaises(ValueError):
            LruCache(0)

    def test_a_negative_capacity_is_refused(self):
        with self.assertRaises(ValueError):
            LruCache(-1)


if __name__ == "__main__":
    unittest.main()
