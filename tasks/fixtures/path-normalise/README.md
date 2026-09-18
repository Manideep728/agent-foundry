# paths

A textual path normaliser.

`paths.normalise.normalise` collapses a relative path without touching the
filesystem, so it can be used to decide whether a path is safe *before*
anything is opened.

Rules it is meant to honour:

- A `.` segment means "here" and is dropped.
- A `..` segment cancels the segment before it.
- **A `..` with nothing left to cancel escapes the base, and is a
  `ValueError`.** This is the whole point of the function: refusing the path
  is the safety property, and silently resolving it to something inside the
  base is worse than not checking at all.
- Repeated slashes collapse, and a trailing slash is dropped.
- An absolute path is a `ValueError`; this normalises relative paths only.
- The empty path, and a path that collapses to nothing, normalise to `""`.
