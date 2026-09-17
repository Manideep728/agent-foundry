"""Merging overlapping intervals."""


def merge(intervals):
    """Merge overlapping and touching intervals.

    Takes [start, end] pairs and returns them sorted by start, with any that
    overlap or touch combined into one. Two intervals touch when one ends
    exactly where the next begins, so [1, 3] and [3, 5] become [1, 5].
    Genuinely disjoint intervals are left alone. The argument is never
    modified, and the returned pairs do not alias it.
    """
    ordered = sorted(intervals)
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
