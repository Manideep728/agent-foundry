"""A least-recently-used cache."""

from collections import OrderedDict


class LruCache:
    """Holds at most `capacity` entries, discarding the least recently used.

    Both reading and writing count as using an entry: an entry returned by
    `get` has just been used, so it becomes the most recently used one and is
    not the next candidate for eviction. Reading an absent key stores nothing.
    """

    def __init__(self, capacity):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._entries = OrderedDict()

    def get(self, key, default=None):
        if key not in self._entries:
            return default
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key, value):
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = value
        if len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def keys(self):
        """Keys from least to most recently used."""
        return list(self._entries)

    def __len__(self):
        return len(self._entries)
