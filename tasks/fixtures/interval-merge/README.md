# intervals

A minimal interval merger.

`intervals.merge.merge` takes a list of `[start, end]` pairs and returns the
merged set.

Rules it is meant to honour:

- The result is sorted by start. The argument need not be sorted.
- Intervals that overlap are combined into one.
- Intervals that *touch* are also combined: one ending exactly where the next
  begins is a single span, so `[1, 3]` and `[3, 5]` become `[1, 5]`.
- Intervals that are genuinely disjoint are left alone.
- The argument is never modified, and the returned pairs do not alias it.
