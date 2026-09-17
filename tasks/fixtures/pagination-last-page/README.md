# paging

A minimal pagination helper.

`paging.pages.paginate` takes a sequence and a page size, and returns the pages.

Rules it is meant to honour:

- Every entry appears exactly once, in its original order.
- Each page holds at most `per_page` entries.
- When the count does not divide evenly, the final page holds the remainder
  and is shorter than the others. It is never dropped.
- An empty sequence produces no pages at all.
- A `per_page` that is not positive is a `ValueError`.
