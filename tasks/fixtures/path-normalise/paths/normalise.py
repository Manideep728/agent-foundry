"""Collapsing a relative path without touching the filesystem."""


def normalise(path):
    """Collapse `.` and `..` in a relative path.

    A `.` segment is dropped and a `..` segment cancels the one before it.
    A `..` with nothing left to cancel would escape the base, and is refused
    with a ValueError rather than resolved to something inside it. Repeated
    slashes collapse, a trailing slash is dropped, and an absolute path is
    refused because this normalises relative paths only.
    """
    if path.startswith("/"):
        raise ValueError(f"not a relative path: {path!r}")

    resolved = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(part)
    return "/".join(resolved)
