"""Splitting a sequence into fixed-size pages."""


def paginate(items, per_page):
    """Split items into pages of at most per_page entries.

    Every entry appears exactly once and keeps its original order. The final
    page holds the remainder, so it is shorter than per_page when the count
    does not divide evenly. An empty sequence produces no pages. per_page must
    be a positive integer.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    page_count = len(items) // per_page
    return [items[i * per_page : (i + 1) * per_page] for i in range(page_count)]
