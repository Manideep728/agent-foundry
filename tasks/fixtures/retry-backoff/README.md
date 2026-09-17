# retry

A minimal exponential-backoff schedule.

`retry.backoff.delays` returns the waits to insert *between* attempts, in
milliseconds.

Rules it is meant to honour:

- The waits go between attempts, so `attempts` tries need `attempts - 1` waits.
  A single attempt waits not at all.
- Each wait is double the one before it, starting at `base_ms`.
- No wait ever exceeds `cap_ms`. Once the doubling reaches the cap, every
  later wait stays at the cap.
- `attempts` below 1, and a `base_ms` or `cap_ms` that is not positive, are
  each a `ValueError`.
- Every wait is a whole number of milliseconds. This schedule is measured, and
  a measurement in this project is never a float.
