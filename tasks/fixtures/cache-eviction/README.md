# lru

A minimal least-recently-used cache.

`lru.cache.LruCache` holds at most `capacity` entries and discards the least
recently used one when it needs room.

Rules it is meant to honour:

- `put` stores a value. Storing a key that is already present updates it.
- `get` returns a stored value, or the default when the key is absent.
- **Both `get` and `put` count as using an entry.** An entry that was read
  recently is not the least recently used one, so it is not the one evicted.
- Reading an absent key does not store anything.
- The cache never holds more than `capacity` entries.
- A capacity that is not positive is a `ValueError`.
